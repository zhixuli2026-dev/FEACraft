"""Run source-built Code_Aster in isolated Linux workers with FS or authenticated S3."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
from io import BytesIO
from ipaddress import IPv4Address
import json
from pathlib import Path
import re
import subprocess
import tempfile
from zipfile import ZipFile

import aiohttp

from linux_solver_isolation_acceptance import fetch
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, docker

SERVER = Path(__file__).resolve().parent
FIXTURE = SERVER/'examples/code_aster/axial'
RUNTIME = '/opt/code-aster-source/bin/run_aster'
H5DUMP = '/opt/vibeflow-ff-c08/code-aster/bin/h5dump'
FIELD = '/CHA/solutionDEPL/0000000000000000000100000000000000000001/NOE/MED_NO_PROFILE_INTERNAL/CO'
# Code_Aster serializes the study globals when saving a native base.
PROBE = '''def check_confinement():
    import os
    import socket
    from pathlib import Path

    assert Path('/case').is_dir() and not Path('/work/data/host-marker').exists()
    assert not Path('/mnt/d').exists()
    assert os.statvfs(os.environ['RUNASTER_ROOT']).f_flag & os.ST_RDONLY
    status = Path('/proc/self/status').read_text()
    assert 'CapEff:\\t0000000000000000' in status and 'NoNewPrivs:\\t1' in status
    with socket.socket() as client:
        client.settimeout(2)
        assert client.connect_ex(('NETWORK_CONTROL', 6379)) != 0
    print('VIBEFLOW_CODE_ASTER_NET='+os.readlink('/proc/self/ns/net'), flush=True)
    print('VIBEFLOW_CODE_ASTER_CONFINED=1', flush=True)

check_confinement()
del check_confinement

'''


def state_summary(data, engine):
    with ZipFile(BytesIO(data)) as archive:
        assert set(archive.namelist())=={'base.json','glob.1','pick.code_aster.infos',
                                       'pick.code_aster.objects','pick.code_aster.sha'}
        assert archive.testzip() is None
        header = json.loads(archive.read('base.json'))
        assert header['schema']=='vibeflow.code-aster-base.v1' and header['runtime_package_sha256']==engine
    return hashlib.sha256(data).hexdigest()


def displacement(path, data, dataset):
    path.write_bytes(data)
    command = [H5DUMP]
    native = str(path)
    if path.drive:
        command[:0] = ['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec']
        native = '/mnt/'+path.drive[0].lower()+path.as_posix()[2:]
    dump = subprocess.run([*command,'-y','-m','%.17g','-d',dataset,native],
                         check=True,capture_output=True,encoding='utf8',timeout=40).stdout
    values = [float(value) for value in re.search(r'\bDATA\s*\{([^}]+)\}',dump).group(1).split(',')]
    assert len(values)==6 and values[:1]+values[2:]==[0]*5
    return values


async def put(client, endpoint, owner, files):
    token, case = owner
    for path, content in files.items():
        await request(client,endpoint,'PUT',f'/cases/{case}/file',token=token,json={'path':path,'content':content})


async def solve(client, endpoint, owner, path, scratch, *, restart=None):
    token, case = owner
    body = {'input_path':path}
    if restart is not None:
        body['restart'] = restart
    job_id = (await request(client,endpoint,'POST',f'/cases/{case}/code-aster:solve',token=token,json=body))['job_id']
    async with asyncio.timeout(160):
        while True:
            job = await request(client,endpoint,'GET',f'/jobs/{job_id}',token=token)
            if job['ended_at'] is not None and not job['completion_pending']:
                break
            await asyncio.sleep(.5)
    if job['status']!='succeeded' or job['exit_code']!=0:
        log = await request(client,endpoint,'GET',f'/jobs/{job_id}/log',token=token,params={'limit':1048576})
        diagnostic = scratch/f'failed-job-{job_id}.log'
        diagnostic.write_text(log['chunk'],encoding='utf-8')
        raise AssertionError((path,job['status'],str(diagnostic)))
    attempt = job['attempt']
    child, = attempt['child_processes']['children'].values()
    identity = child['identity']
    assert child['purpose']=='code_aster_solve_linux' and identity['host']['uid']==65534, child
    assert identity['namespace_init']['pid_namespace']!=identity['host']['pid_namespace'], identity
    assert identity['linux_network']['host']!=identity['linux_network']['init'], identity
    for stop in (attempt['process_stop'],child['stop']):
        assert stop['verified'] and stop['scope_empty'], stop
    published = attempt['outputs']['publication']
    assert published['status']=='committed', published
    print('ASTER_ISOLATED_JOB', path, job_id, flush=True)
    return {'job':job_id,'publication':published['id'],'stop':child['stop'],
            'network':identity['linux_network'],'child_uid':identity['host']['uid'],'inputs':attempt['workspace']}


async def solution(client, endpoint, owner, job, scratch, name, *, resumed=False):
    base = '.vibeflow/publications/'+job['publication']
    manifest = json.loads(await fetch(client, endpoint, owner, base+'/manifest.json'))
    assert manifest['descriptor']['executable']==RUNTIME
    data, message = await asyncio.gather(*(fetch(client,endpoint,owner,name+suffix) for suffix in ('.rmed','.mess')))
    assert re.search(rb'^VIBEFLOW_CODE_ASTER_CONFINED=1$',message,re.M), name
    network = re.search(rb'^VIBEFLOW_CODE_ASTER_NET=net:\[(\d+)\]$',message,re.M)
    assert network and network[1].decode('ascii')==job['network']['init'], (name,network,job['network'])
    for suffix, content in (('.rmed',data),('.mess',message)):
        member = name+suffix
        assert manifest['files'][member]['sha256']==hashlib.sha256(content).hexdigest()
        assert await fetch(client,endpoint,owner,base+'/files/'+member)==content
    dataset = FIELD.replace('solutionDEPL','resumed_DEPL') if resumed else FIELD
    return await asyncio.to_thread(displacement,scratch/(name+'.rmed'),data,dataset)


async def verify(endpoint, peer, scratch, evidence, engine, network_control):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45),trust_env=False) as client:
        direct_owner = await account(client,endpoint)
        resume_owner = await account(client,endpoint)
        export = (FIXTURE/'axial.export').read_text(encoding='ascii')
        probe = PROBE.replace('NETWORK_CONTROL', network_control)
        deck = probe+(FIXTURE/'axial.comm').read_text(encoding='ascii')
        mesh = (FIXTURE/'axial.mail').read_text(encoding='ascii')
        await put(client,endpoint,direct_owner,{'axial.export':export,'axial.mail':mesh,
                                                 'axial.comm':deck.replace('FX=2.1e6','FX=4.2e6')})
        await put(client,endpoint,resume_owner,{'axial.export':export+'\nR base state R 0\n',
                                                 'axial.mail':mesh,'axial.comm':deck})
        full, first = await asyncio.gather(solve(client,endpoint,direct_owner,'axial.export',scratch),
                                           solve(client,endpoint,resume_owner,'axial.export',scratch))
        first_field = await solution(client,peer,resume_owner,first,scratch,'axial')
        assert abs(first_field[1]-.001)<1e-12, first_field
        archive = 'state_base.zip'
        original = await fetch(client,peer,resume_owner,archive)
        old_digest = state_summary(original,engine)
        await put(client,endpoint,resume_owner,{'axial.comm':deck.replace('E=210e9','E=105e9')})
        newer = await solve(client,endpoint,resume_owner,'axial.export',scratch)
        assert newer['publication']!=first['publication']
        new_digest = state_summary(await fetch(client,peer,resume_owner,archive),engine)
        assert new_digest!=old_digest
        await put(client,endpoint,resume_owner,{'continue.export':(FIXTURE/'continue.export').read_text(encoding='ascii'),
                                                 'continue.comm':probe+(FIXTURE/'continue.comm').read_text(encoding='ascii')})
        restart = {'publication_id':first['publication'],'checkpoint_path':archive}
        resumed = await solve(client,endpoint,resume_owner,'continue.export',scratch,restart=restart)
        source, = resumed['inputs']['publications']
        assert source['publication_id']==first['publication'] and source['require_current'] is False
        assert resumed['inputs']['files'][archive]['sha256']==old_digest
        direct = await solution(client,peer,direct_owner,full,scratch,'axial')
        recovered = await solution(client,peer,resume_owner,resumed,scratch,'restored',resumed=True)
        assert abs(direct[1]-.002)<1e-12 and recovered==direct
        assert state_summary(await fetch(client,peer,resume_owner,archive),engine)==new_digest
        continued = state_summary(await fetch(client,peer,resume_owner,'continued-state_base.zip'),engine)
        assert continued not in {old_digest,new_digest}
        evidence.update(jobs=[full,first,newer,resumed],original_base_sha256=old_digest,new_base_sha256=new_digest,
                        continued_base_sha256=continued,base_tip_m=first_field[1],resumed_tip_m=recovered[1],
                        max_field_error_m=max(abs(x-y) for x,y in zip(recovered,direct,strict=True)),
                        runtime_package_sha256=engine,peer_publication_verified=True)


def main(args):
    root = Path(tempfile.mkdtemp(prefix='j16-code-aster-',dir=SERVER.parents[1]/'tmp'))
    print('ASTER_ISOLATION_EVIDENCE',root,flush=True)
    original = set(docker('ps','-q','--no-trunc').splitlines())
    evidence = {'passed':False,'storage':args.storage,'actual_model_calls':0,'simulated_model_calls':0}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack,root,args.api_image,WEB_IMAGE)
            docker('stop',deployment.worker)
            if args.storage=='s3':
                deployment.configure_s3(root)
            runtime = deployment.volume('aster-runtime','/opt')
            archive = args.bundle.resolve(strict=True)
            installer = deployment.run_container(deployment.label+'-aster-installer','--mount',runtime,
                '--mount',deployment.data,'--mount',f'type=bind,source={archive},target=/bundle.tar,readonly',
                '--entrypoint','/bin/sh',args.api_image,'-ec','tar -xf /bundle.tar -C /opt; '
                'test -x /opt/code-aster-source/bin/run_aster; install -d -o 65534 -g 65534 /work/data/files; '
                'touch /work/data/host-marker; sha256sum /opt/code-aster-source/feacraft-code-aster-source.json')
            status = docker('wait',installer,timeout=210)
            assert status=='0',docker('logs',installer,timeout=10)
            engine = docker('logs',installer,timeout=10).split()[0]
            assert re.fullmatch('[0-9a-f]{64}',engine)
            deployment.runtime.extend(('--mount',runtime+',readonly'))
            deployment.env['VIBEFLOW_CODE_ASTER'] = RUNTIME
            deployment.flags.extend(('-e','VIBEFLOW_CODE_ASTER'))
            worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec',worker,'id','-u')=='65534'
            docker('exec',worker,'test','-f','/work/data/host-marker')
            network_control = str(IPv4Address(docker('inspect','-f',
                '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',deployment.redis)))
            docker('exec',worker,'python','-c',
                   'import socket,sys; socket.create_connection((sys.argv[1],6379),timeout=3).close()',network_control)
            _,endpoint = deployment.api(deployment.a)
            if args.storage=='s3':
                cache = deployment.volume('cold-peer-cache','/work/data/files')
                docker('run','--rm','--network','none','--pull=never','--mount',cache,
                       '--entrypoint','/usr/bin/chown',args.api_image,'65534:65534','/work/data/files')
                deployment.runtime.extend(('--mount',cache))
            reader,peer = deployment.api(deployment.b)
            if args.storage=='s3':
                assert docker('exec',reader,'find','/work/data/files','-type','f')==''
            asyncio.run(verify(endpoint,peer,root,evidence,engine,network_control))
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:900]
        raise
    finally:
        evidence['original_services'] = original==set(docker('ps','-q','--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (root/'result.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        print('ASTER_ISOLATION_RESULT',evidence['passed'],flush=True)
    assert evidence['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--storage',choices=('fs','s3'),required=True)
    parser.add_argument('--api-image',default=API_IMAGE)
    main(parser.parse_args())
