"""Run source-built MOOSE jobs in isolated Linux workers using FS or authenticated S3."""

import argparse
import asyncio
from contextlib import ExitStack
import csv
import hashlib
from io import BytesIO
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
RUNTIME = '/work/moose-package/bin/combined-opt'
DECK = (SERVER/'examples/moose/thermal/transient.i').read_text(encoding='ascii')
DUMP = '/opt/vibeflow-ff-c08/moose/bin/ncdump'


def fields(scratch, name, data):
    path = scratch/(name+'.e')
    path.write_bytes(data)
    command = [DUMP]
    native = str(path)
    if path.drive:
        command[:0] = ['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec']
        native = '/mnt/'+path.drive[0].lower()+path.as_posix()[2:]
    result = subprocess.run([*command, '-p', '17,17', '-v', 'time_whole,coordx,vals_nod_var1', native],
        check=True, capture_output=True, encoding='utf-8', timeout=30)
    data = result.stdout.split('data:', 1)[1]
    names = dict(re.findall(r'^\s*(time_whole|coordx|vals_nod_var1)\s*=\s*([^;]+);', data, re.M))
    times, coords, nodal = ([float(n) for n in names[key].split(',')]
                            for key in ('time_whole','coordx','vals_nod_var1'))
    assert coords == [i/10 for i in range(11)] and len(nodal) == len(times)*len(coords)
    return [round(t,12) for t in times], nodal


async def solve(client, endpoint, owner, name, deck, *, restart=None):
    token, case = owner
    path = name+'.i'
    await request(client, endpoint, 'PUT', f'/cases/{case}/file', token=token,
                  json={'path': path, 'content': deck})
    params = {'input_path': path}
    if restart is not None:
        params['restart'] = restart
    job_id = (await request(client, endpoint, 'POST', f'/cases/{case}/moose:solve', token=token, json=params))['job_id']
    async with asyncio.timeout(150):
        while True:
            job = await request(client, endpoint, 'GET', f'/jobs/{job_id}', token=token)
            if job['ended_at'] is not None and not job['completion_pending']:
                break
            await asyncio.sleep(.4)
    assert job['status']=='succeeded' and job['exit_code']==0, (name,job['status'],job['log_backlog'][-2000:])
    attempt = job['attempt']
    child, = attempt['child_processes']['children'].values()
    assert child['purpose']=='moose_solve_linux' and child['identity']['host']['uid']==65534, child
    for stop in (attempt['process_stop'], child['stop']):
        assert stop['verified'] and stop['scope_empty'], stop
    identity = child['identity']
    assert identity['linux_network']['host'] != identity['linux_network']['init'], identity
    assert identity['namespace_init']['pid_namespace'] != identity['host']['pid_namespace'], identity
    publication = attempt['outputs']['publication']
    assert publication['status']=='committed', publication
    print('MOOSE_ISOLATED_JOB', name, job_id, flush=True)
    return {'job':job_id,'publication':publication['id'],'stop':child['stop'],
            'network':identity['linux_network'],'child_uid':identity['host']['uid'],
            'inputs':attempt['workspace']}


async def verify(endpoint, peer, scratch, evidence):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40),trust_env=False) as client:
        full_owner = await account(client, endpoint)
        resume_owner = await account(client, endpoint)
        full, first = await asyncio.gather(
            solve(client, endpoint, full_owner, 'continuous', DECK.replace('checkpoint = true','checkpoint = false')),
            solve(client, endpoint, resume_owner, 'part', DECK.replace('end_time = 0.4','end_time = 0.2')))
        checkpoint = 'part_checkpoint.zip'
        original = await fetch(client, peer, resume_owner, checkpoint)
        digest = hashlib.sha256(original).hexdigest()
        with ZipFile(BytesIO(original)) as bundle:
            assert bundle.testzip() is None and len(bundle.namelist())==7
            assert json.loads(bundle.read('checkpoint.json'))['step']=='0002'
        newer = await solve(client, endpoint, resume_owner, 'part', DECK.replace('end_time = 0.4','end_time = 0.3'))
        assert newer['publication'] != first['publication'] and await fetch(client, peer, resume_owner, checkpoint) != original
        restart = {'publication_id':first['publication'],'checkpoint_path':checkpoint}
        resumed = await solve(client, endpoint, resume_owner, 'continued', DECK, restart=restart)
        source, = resumed['inputs']['publications']
        assert source['publication_id']==first['publication'] and source['require_current'] is False, source
        assert resumed['inputs']['files'][checkpoint]['sha256']==digest
        measurements = []
        for owner, name, row in ((full_owner, 'continuous', full), (resume_owner, 'continued', resumed)):
            base = '.vibeflow/publications/'+row['publication']
            manifest = json.loads(await fetch(client, peer, owner, base+'/manifest.json'))
            assert manifest['descriptor']['executable']==RUNTIME
            grid = await fetch(client, peer, owner, name+'_out.e')
            table = await fetch(client, peer, owner, name+'_out.csv')
            for file, content in ((name+'_out.e', grid), (name+'_out.csv', table)):
                assert hashlib.sha256(content).hexdigest()==manifest['files'][file]['sha256']
                assert content==await fetch(client, peer, owner, base+'/files/'+file)
            times, nodal = await asyncio.to_thread(fields, scratch, name, grid)
            rows = list(csv.DictReader(table.decode('ascii').splitlines()))
            assert [round(float(item['time']),12) for item in rows]==times
            measurements.append((times,nodal,[float(item['midpoint']) for item in rows]))
        (full_times, full_nodes, full_midpoints), (resumed_times, resumed_nodes, resumed_midpoints) = measurements
        assert full_times==[0,.1,.2,.3,.4] and resumed_times==[.2,.3,.4]
        error = max(abs(a-b) for a,b in zip(full_nodes[-len(resumed_nodes):], resumed_nodes, strict=True))
        assert error<1e-10 and resumed_midpoints==full_midpoints[-3:]
        evidence.update(jobs=[full,first,newer,resumed],checkpoint_sha256=digest,
                        final_midpoint=resumed_midpoints[-1],max_node_field_error=error,
                        frozen_checkpoint_used=True,peer_publication_verified=True)


def main(args):
    root = Path(tempfile.mkdtemp(prefix='j16-moose-', dir=SERVER.parents[1]/'tmp'))
    print('MOOSE_ISOLATION_EVIDENCE', root, flush=True)
    original = set(docker('ps','-q','--no-trunc').splitlines())
    evidence = {'passed':False,'storage':args.storage,'actual_model_calls':0,'simulated_model_calls':0}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack, root, API_IMAGE, WEB_IMAGE)
            docker('stop', deployment.worker)
            if args.storage == 's3':
                deployment.configure_s3(root)
            runtime = deployment.volume('moose-package', '/work/moose-package')
            dependencies = deployment.volume('moose-dependencies', '/opt')
            archive = args.bundle.resolve(strict=True)
            installer = deployment.run_container(deployment.label+'-moose-installer','--mount',runtime,'--mount',dependencies,
                   '--mount',deployment.data,'--mount',f'type=bind,source={archive},target=/bundle.tar.gz,readonly',
                   '--entrypoint','/bin/sh',API_IMAGE,'-ec',
                   'tar -xzf /bundle.tar.gz -C /work/moose-package --strip-components=1 package; '
                   'tar -xzf /bundle.tar.gz -C /opt --strip-components=1 opt; '
                   'cd /work/moose-package; sha256sum --quiet -c SHA256SUMS; '
                   'install -d -o 65534 -g 65534 /work/data/files')
            status = docker('wait',installer,timeout=180)
            assert status=='0', docker('logs',installer,timeout=10)
            deployment.runtime.extend(('--mount',runtime+',readonly','--mount',dependencies+',readonly'))
            deployment.env['VIBEFLOW_MOOSE'] = RUNTIME
            deployment.flags.extend(('-e','VIBEFLOW_MOOSE'))
            worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec',worker,'id','-u')=='65534'
            docker('exec',worker,RUNTIME,'--version')
            _, endpoint = deployment.api(deployment.a)
            if args.storage == 's3':
                cache = deployment.volume('cold-peer-cache','/work/data/files')
                docker('run','--rm','--network','none','--pull=never','--mount',cache,
                       '--entrypoint','/usr/bin/chown',API_IMAGE,'65534:65534','/work/data/files')
                deployment.runtime.extend(('--mount',cache))
            reader, peer = deployment.api(deployment.b)
            if args.storage == 's3':
                assert docker('exec',reader,'find','/work/data/files','-type','f')==''
            asyncio.run(verify(endpoint,peer,root,evidence))
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:850]
        raise
    finally:
        evidence['original_services'] = original==set(docker('ps','-q','--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (root/'result.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        print('MOOSE_ISOLATION_RESULT', evidence['passed'], flush=True)
    assert evidence['passed'], evidence


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--storage',choices=('fs','s3'),required=True)
    main(parser.parse_args())
