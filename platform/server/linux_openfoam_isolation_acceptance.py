"""Run registered Linux OpenFOAM mesh, serial solve and MPI jobs on FS or authenticated S3."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile

import aiohttp

from app.services.casedict import set_block
from cae_fixtures import cavity_2d
from linux_solver_isolation_acceptance import fetch
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import WEB_IMAGE, Deployment, docker


IMAGE = 'vibeflow/api:linux-mpi-udf-v4-20261118'
RUN = 'C-run/case-1'
FIELD = RUN+'/0.5/U'
BOUNDARY = '''import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory

from app.services.linux_job_workspace import child_environment
from app.services.openfoam_runtime import isolated_argv

host = sys.argv[1]
with socket.create_connection((host, 6379), timeout=2):
    pass
with TemporaryDirectory(dir='/work/data/files') as folder:
    root = Path(folder)
    run = root/'run'
    run.mkdir()
    (root/'manifest.json').write_text('manifest')
    (root/'sibling').touch()
    body = """import os,socket,sys
from pathlib import Path
root=Path.cwd()
assert not (root.parent/'sibling').exists()
assert os.statvfs(root.parent/'manifest.json').f_flag & os.ST_RDONLY
assert (root.parent/'manifest.json').read_text() == 'manifest'
assert 'OPENFOAM_HOST_CANARY' not in os.environ
assert os.readlink('/proc/self/ns/net') != sys.argv[2]
assert [name for _,name in socket.if_nameindex()] == ['lo']
with socket.socket() as external:
    external.settimeout(2)
    assert external.connect_ex((sys.argv[1],6379)) != 0
with socket.socket() as server:
    server.bind(('127.0.0.1',0))
    server.listen()
    with socket.create_connection(server.getsockname(),timeout=2):
        server.accept()[0].close()
(root/'written').write_text('owned')
"""
    command = shlex.join([sys.executable,'-I','-S','-c',body,host,os.readlink('/proc/self/ns/net')])
    argv = isolated_argv(run, ['/bin/bash','--noprofile','--norc','-c',command])
    subprocess.run(argv, cwd=run, env=child_environment(dict(os.environ,OPENFOAM_HOST_CANARY='host-only')), check=True)
    assert (run/'written').read_text()=='owned'
print('OPENFOAM_BOUNDARY_VERIFIED')
'''
LIVE_RANKS = '''import os,sys
from pathlib import Path

root = int(sys.argv[1])
parents = {}
ranks = []
for process in Path('/proc').iterdir():
    if not process.name.isdecimal():
        continue
    try:
        status = (process/'status').read_text()
        argv = (process/'cmdline').read_bytes().split(b'\\0')
    except (OSError, ProcessLookupError):
        continue
    pid = int(process.name)
    parents[pid] = int(next(row for row in status.splitlines() if row.startswith('PPid:')).split()[1])
    if os.path.basename(argv[0]) == b'icoFoam' and b'-parallel' in argv:
        ranks.append(pid)
count = 0
for pid in ranks:
    while pid in parents and pid > 1 and pid != root:
        pid = parents[pid]
    count += pid == root
print(count)
'''


def vectors(data):
    match = re.search(rb'internalField\s+nonuniform\s+List<vector>\s+400\s*\((.*?)\)\s*;',data,re.S)
    assert match, data[:200]
    rows = [tuple(map(float,row.split())) for row in re.findall(rb'\(([^()]*)\)',match[1])]
    assert len(rows)==400 and all(len(row)==3 and all(math.isfinite(value) for value in row) for row in rows)
    return rows


async def finished(client, endpoint, token, job_id, scratch, *, expected='succeeded'):
    async with asyncio.timeout(140):
        while True:
            job = await request(client,endpoint,'GET',f'/jobs/{job_id}',token=token)
            if job['ended_at'] is not None and not job['completion_pending']:
                # The cancel route records the decision before the remote worker drains.
                if expected!='cancelled' or job['status']!=expected or job['attempt'].get('process_stop') is not None:
                    break
            await asyncio.sleep(.25)
    if job['status']!=expected:
        log = await request(client,endpoint,'GET',f'/jobs/{job_id}/log',token=token,params={'limit':1048576})
        path = scratch/f'job-{job_id}.log'
        path.write_text(log['chunk'],encoding='utf-8')
        raise AssertionError((job_id,job['status'],str(path),job['log_backlog'][-600:]))
    stop = job['attempt']['process_stop']
    assert stop['verified'] and stop['scope_empty']
    if expected=='succeeded':
        assert job['exit_code']==0
    return job


def namespace(record):
    assert record['host']['uid']==65534 and record['namespace_init']['uid']==65534, record
    assert record['namespace_init']['pid_namespace']!=record['host']['pid_namespace'], record
    assert record['linux_network']['host']!=record['linux_network']['init'], record
    return record['linux_network']


async def registered_flow(client, endpoint, owner, scratch):
    token,case = owner
    prefix = f'/cases/{case}'
    current = await request(client,endpoint,'GET',prefix+'/caseDict',token=token)
    source = current['content']
    for name, value in {
        'caseMeta':{'discipline':'fluid','analysisType':'transient','dimensions':'2D','units':{'length':'m'}},
        'geometry':{'generator':'cadquery','parameters':{'lx':.1,'ly':.1,'lz':.01}},
        'mesh':{'source':'blockMesh','qualityTargets':{'policy':'fail','maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100}},
        'numericalMethod':{'solver':'icoFoam'},'postProcessing':{'runDir':RUN},
    }.items():
        source = set_block(source,name,value)
    await request(client,endpoint,'PUT',prefix+'/caseDict',token=token,
                  json={'content':source,'expected_revision':current['dict_revision']})
    for name, data in cavity_2d().items():
        await request(client,endpoint,'PUT',prefix+'/file',token=token,json={'path':name,'content':data})
    geometry = 'import cadquery as cq\ndef build_geometry(ctx):\n    return cq.Workplane("XY").box(.1,.1,.01)\n'
    seeded = await request(client,endpoint,'PUT',prefix+'/file',token=token,
                           json={'path':'A-geo/geometry.py','content':geometry})
    if seeded.get('auto_preview_job'):
        await finished(client,endpoint,token,seeded['auto_preview_job'],scratch)
    current = await request(client,endpoint,'GET',prefix+'/caseDict',token=token)
    flow = await request(client,endpoint,'POST',prefix+'/workflow-runs',token=token,json={
        'target':'solve','run_dir':RUN,'expected_revision':current['dict_revision'],
        'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
    path = '/workflow-runs/'+flow['id']
    jobs = {}
    confirmations = []
    async with asyncio.timeout(190):
        while flow['status']!='succeeded':
            flow = await request(client,endpoint,'GET',path,token=token)
            for step in flow['steps']:
                if step['attempts'] and step['key'] not in jobs and step['status'] in {'succeeded','failed'}:
                    jobs[step['key']] = await finished(client,endpoint,token,step['attempts'][-1]['job_id'],scratch)
                if step['status']=='awaiting_confirmation':
                    await request(client,endpoint,'POST',prefix+'/caseDict:advance',token=token,json={
                        'state':step['confirmation_state'],'confirmed':True,'expected_revision':step['confirmation_revision'],
                        'workflow_step_id':step['id'],'expected_step_version':step['version']})
                    confirmations.append(step['confirmation_state'])
                    break
            assert flow['status'] not in {'failed','interrupted','cancelled'}, flow['status']
            await asyncio.sleep(.25)
    assert confirmations==['B','C','D'], confirmations
    return jobs


async def cancel_mpi(client, endpoint, peer, owner, scratch, published, worker):
    token,case = owner
    control = RUN+'/system/controlDict'
    original = (await fetch(client,peer,owner,control)).decode('utf-8')
    extended, count = re.subn(r'(endTime\s+)0\.5;',r'\g<1>200;',original)
    assert count==1
    await request(client,endpoint,'PUT',f'/cases/{case}/file',token=token,json={'path':control,'content':extended})
    assert await fetch(client,peer,owner,control)==extended.encode('utf-8')
    pending = await request(client,endpoint,'POST',f'/cases/{case}/mpi:run',token=token,json={
        'run_dir':RUN,'solver':'icoFoam','subdomains':2,'force':True}, timeout=aiohttp.ClientTimeout(total=150))
    running_path = f'/jobs/{pending["job_id"]}'
    async with asyncio.timeout(55):
        while True:
            running = await request(client,peer,'GET',running_path,token=token)
            if running['ended_at'] is not None:
                (scratch/'unexpected-mpi-stop.json').write_text(json.dumps(running,indent=2),encoding='utf-8')
                await finished(client,peer,token,pending['job_id'],scratch,expected='cancelled')
                raise AssertionError('MPI exited before the test requested cancellation')
            children = running['attempt']['child_processes']['children']
            mpi = next((item for item in children.values()
                        if item['purpose']=='mpi_run_linux' and item['phase']=='running'), None)
            if mpi is not None:
                count = await asyncio.to_thread(docker,'exec',worker,'python','-B','-c',LIVE_RANKS,
                                                str(mpi['identity']['host']['pid']))
                if int(count)==2:
                    break
            await asyncio.sleep(.2)
    result = await request(client,peer,'POST',running_path+':cancel',token=token)
    assert result['ok'], result
    stopped = (await finished(client,peer,token,pending['job_id'],scratch,expected='cancelled'))['attempt']
    cancelled, = stopped['child_processes']['children'].values()
    assert cancelled['stop']['verified'] and cancelled['stop']['scope_empty']
    assert stopped['outputs'].get('publication',{}).get('status')!='committed'
    assert await fetch(client,peer,owner,FIELD)==published
    await request(client,endpoint,'PUT',f'/cases/{case}/file',token=token,json={'path':control,'content':original})
    return {'job':pending['job_id'],'process_stop':stopped['process_stop'],
            'child_stop':cancelled['stop'],'original_field_preserved':True,'observed_live_ranks':2}


async def verify(endpoint, peer, scratch, evidence, worker):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35),trust_env=False) as client:
        owner = await account(client,endpoint)
        token,case = owner
        jobs = await registered_flow(client,endpoint,owner,scratch)
        for key in ('mesh','solve'):
            candidate = jobs[key]['attempt']
            evidence[key] = {'job':jobs[key]['id'],'stop':candidate['process_stop'],
                             'network':namespace(candidate['process_identity']),
                             'publication':candidate['outputs']['publication']['id']}
        quality = json.loads(await fetch(client,peer,owner,'B-mesh/quality.json'))
        assert quality['mesh_ok'] and quality['status']=='pass', quality
        serial = await fetch(client,peer,owner,FIELD)
        evidence['mpi_cancel'] = await cancel_mpi(client,endpoint,peer,owner,scratch,serial,worker)
        parallel = await request(client,endpoint,'POST',f'/cases/{case}/mpi:run',token=token,json={
            'run_dir':RUN,'solver':'icoFoam','subdomains':2,'force':True}, timeout=aiohttp.ClientTimeout(total=150))
        job = await finished(client,endpoint,token,parallel['job_id'],scratch)
        attempt = job['attempt']
        child, = attempt['child_processes']['children'].values()
        assert child['purpose']=='mpi_run_linux' and child['stop']['verified'] and child['stop']['scope_empty'], child
        status = await request(client,peer,'GET',f'/cases/{case}/mpi:status',token=token,params={'run_dir':RUN})
        assert status['processor_dirs']==['processor0','processor1'] and '0.5' in status['times'], status
        reconstructed = await fetch(client,peer,owner,FIELD)
        first, second = vectors(serial), vectors(reconstructed)
        error = max(abs(a-b) for row, other in zip(first,second,strict=True) for a,b in zip(row,other,strict=True))
        error /= max(1,max(abs(value) for row in first for value in row))
        assert error<=1e-5, error
        evidence['mpi'] = {'job':job['id'],'stop':attempt['process_stop'],'child_stop':child['stop'],
                           'network':namespace(child['identity']),'relative_velocity_error':error,'status':status,
                           'serial_sha256':hashlib.sha256(serial).hexdigest(),
                           'reconstructed_sha256':hashlib.sha256(reconstructed).hexdigest()}
        outsider, _ = await account(client,endpoint)
        await request(client,peer,'GET',f'/jobs/{job["id"]}',token=outsider,expected=404)


def main(args):
    root = Path(tempfile.mkdtemp(prefix='j16-openfoam-',dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('OPENFOAM_ISOLATION_EVIDENCE',root,flush=True)
    original = set(docker('ps','-q','--no-trunc').splitlines())
    evidence = {'passed':False,'storage':args.storage,'actual_model_calls':0,'simulated_model_calls':0}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack,root,args.api_image,WEB_IMAGE)
            docker('stop',deployment.worker)
            if args.storage=='s3':
                deployment.configure_s3(root)
            docker('run','--rm','--network','none','--mount',deployment.data,'--entrypoint','/usr/bin/install',
                   args.api_image,'-d','-o','65534','-g','65534','/work/data/files')
            deployment.worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec',deployment.worker,'id','-u')=='65534'
            redis_ip = docker('inspect','-f','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',deployment.redis)
            assert docker('exec',deployment.worker,'python','-B','-c',BOUNDARY,redis_ip)=='OPENFOAM_BOUNDARY_VERIFIED'
            evidence['boundary'] = {'private_loopback':True,'host_tcp_denied':True,'readonly_manifest':True,
                                    'sibling_hidden':True,'case_write':True}
            _,endpoint = deployment.api(deployment.a)
            if args.storage=='s3':
                cache = deployment.volume('cold-peer-cache','/work/data/files')
                docker('run','--rm','--network','none','--mount',cache,'--entrypoint','/usr/bin/chown',
                       args.api_image,'65534:65534','/work/data/files')
                deployment.runtime.extend(('--mount',cache))
            reader,peer = deployment.api(deployment.b)
            if args.storage=='s3':
                assert docker('exec',reader,'find','/work/data/files','-type','f')==''
            asyncio.run(verify(endpoint,peer,root,evidence,deployment.worker))
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:1200]
        raise
    finally:
        evidence['original_services'] = original==set(docker('ps','-q','--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (root/'result.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        print('OPENFOAM_ISOLATION_RESULT',evidence['passed'],flush=True)
    assert evidence['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage',choices=('fs','s3'),required=True)
    parser.add_argument('--api-image',default=IMAGE)
    main(parser.parse_args())
