"""Verify isolated Linux case commands and real OpenFOAM UDF execution."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import secrets
import shlex
import tempfile

import aiohttp

from linux_geometry_isolation_acceptance import OBSERVE_CHILD, receipt
from linux_openfoam_isolation_acceptance import FIELD, IMAGE, RUN, finished, namespace, registered_flow, vectors
from linux_solver_isolation_acceptance import fetch
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import WEB_IMAGE, Deployment, docker


CANARY = '/work/data/command-host-fixture.txt'
ENV = 'COMMAND_ACCEPTANCE_FIXTURE'
REFERENCE = 'Ref/command-input.txt'
OUTPUT = 'D-post/command-result.txt'
CONTENT = 'isolated case input'
UDF_NAME = 'myFunctionObject'
UDF_DIR = 'UDF/'+UDF_NAME
LIBRARY = UDF_DIR+'/libmyFunctionObject.so'
SOLVER_LOG = RUN+'/udf-solve.log'
CONTROL_DICT = RUN+'/system/controlDict'


async def put(client, api, owner, path, text):
    token, case = owner
    return await request(client, api, 'PUT', f'/cases/{case}/file', token=token,
                         json={'path': path, 'content': text})


async def command_jobs(client, api, peer, owner, redis, scratch):
    token, case = owner
    route = f'/cases/{case}/jobs'
    await put(client, api, owner, REFERENCE, CONTENT)
    check = ('import os,socket,sys; from pathlib import Path; from multiprocessing import shared_memory; '
             'assert Path(sys.argv[1]).read_text()==sys.argv[2]; '
             'assert not Path(sys.argv[3]).exists(); assert sys.argv[4] not in os.environ; '
             's=socket.socket(); s.settimeout(1); assert s.connect_ex((sys.argv[5],6379))!=0; s.close(); '
             'server=socket.create_server(("127.0.0.1",0)); local=socket.create_connection(server.getsockname()); '
             'accepted,_=server.accept(); local.sendall(b"case"); assert accepted.recv(4)==b"case"; '
             'accepted.close(); local.close(); server.close(); '
             'block=shared_memory.SharedMemory(create=True,size=16); block.buf[:4]=b"case"; '
             'assert bytes(block.buf[:4])==b"case"; block.close(); block.unlink(); '
             'print("COMMAND_BOUNDARY_VERIFIED")')
    command = shlex.join(['python3','-I','-c',check,REFERENCE,CONTENT,CANARY,ENV,redis])
    evidence = {}
    for name, envelope in (('default_shell', {}), ('selected_shell', {'input_paths':[REFERENCE]})):
        created = await request(client, api, 'POST', route, token=token, json={'command':command,**envelope})
        job = await finished(client, peer, token, created['id'], scratch)
        assert 'COMMAND_BOUNDARY_VERIFIED' in job['log_backlog']
        evidence[name] = receipt(job)
    created = await request(client, api, 'POST', route, token=token,
                            json={'type':'convert','command':f'mkdir -p D-post && cp {REFERENCE} {OUTPUT}'})
    typed = await finished(client, peer, token, created['id'], scratch)
    publication = typed['attempt']['outputs']['publication']
    assert publication['status'] == 'committed'
    base = f'.vibeflow/publications/{publication["id"]}'
    manifest = json.loads(await fetch(client, peer, owner, base+'/manifest.json'))
    data = await fetch(client, peer, owner, base+'/files/'+OUTPUT)
    assert data == CONTENT.encode() == await fetch(client, peer, owner, OUTPUT)
    assert manifest['files'][OUTPUT]['sha256'] == hashlib.sha256(data).hexdigest()
    evidence['typed'] = {**receipt(typed),'publication':publication['id'],'sha256':hashlib.sha256(data).hexdigest()}
    created = await request(client, api, 'POST', route, token=token, json={
        'command':f'cat {OUTPUT}', 'input_publications':[
            {'publication_id':publication['id'],'paths':[OUTPUT],'require_current':True}]})
    consumed = await finished(client, peer, token, created['id'], scratch)
    assert CONTENT in consumed['log_backlog']
    evidence['publication_input'] = receipt(consumed)
    batch = await request(client, api, 'POST', route+':batch', token=token, expected=202, json={
        'commands':['printf BATCH_ISOLATED_FIRST', 'printf BATCH_ISOLATED_SECOND'], 'max_concurrent':1})
    evidence['batch'] = []
    for item, label in zip(batch['jobs'], ('BATCH_ISOLATED_FIRST','BATCH_ISOLATED_SECOND'), strict=True):
        member = await finished(client, peer, token, item['id'], scratch)
        assert label in member['log_backlog']
        evidence['batch'].append(receipt(member))
    return evidence


def udf_receipt(job):
    child, = job['attempt']['child_processes']['children'].values()
    assert child['purpose'] == 'udf_run_linux'
    assert child['stop']['verified'] and child['stop']['scope_empty']
    return {'job':job['id'],'network':namespace(child['identity']),
            'root_stop':job['attempt']['process_stop'],'child_stop':child['stop']}


async def run_udf(client, api, peer, owner, scratch):
    token, case = owner
    prefix = f'/cases/{case}'
    await registered_flow(client, api, owner, scratch)
    serial = await fetch(client, peer, owner, FIELD)
    initial = (await fetch(client, peer, owner, CONTROL_DICT)).decode('utf-8')
    await request(client, api, 'POST', prefix+'/udf/scaffold', token=token, json={'name':UDF_NAME})
    path = UDF_DIR+'/myFunctionObject.C'
    original = (await fetch(client, peer, owner, path)).decode('utf-8')
    marker = 'COMPILED_UDF_'+secrets.token_hex(8)
    assert 'P0_UDF_OK' in original
    await put(client, api, owner, path, original.replace('P0_UDF_OK',marker))
    body = {'name':UDF_NAME}
    built = await request(client, api, 'POST', prefix+'/udf:build', token=token, json=body)
    build = await finished(client, peer, token, built['job_id'], scratch)
    library = await fetch(client, peer, owner, LIBRARY)
    assert library.startswith(b'\x7fELF')
    assert b'UDF_LIBRARY_LOADED' in await fetch(client, peer, owner, UDF_DIR+'/udf-load.log')
    publication = build['attempt']['outputs']['publication']
    assert publication['status']=='committed'
    base = f'.vibeflow/publications/{publication["id"]}'
    manifest = json.loads(await fetch(client, peer, owner, base+'/manifest.json'))
    assert manifest['files'][LIBRARY]['sha256']==hashlib.sha256(library).hexdigest()
    # Increase written precision to produce a separately archived field while
    # retaining the same physical and discretization inputs.
    source, replacements = re.subn(r'(writePrecision\s+)\d+;',r'\g<1>9;',initial)
    assert replacements==1 and source!=initial
    await put(client, api, owner, CONTROL_DICT, source)
    body['run_dir'] = RUN
    submitted = await request(client, api, 'POST', prefix+'/udf:integrate', token=token, json=body)
    job = await finished(client, peer, token, submitted['job_id'], scratch)
    integrated = job['attempt']['outputs']['publication']
    assert integrated['status']=='committed'
    base = f'.vibeflow/publications/{integrated["id"]}'
    report = json.loads(await fetch(client, peer, owner, base+'/manifest.json'))
    log = await fetch(client, peer, owner, base+'/files/'+SOLVER_LOG)
    assert marker.encode() in log and b'Time = 0.5' in log
    computed = await fetch(client, peer, owner, base+'/files/'+FIELD)
    assert hashlib.sha256(computed).hexdigest()==report['files'][FIELD]['sha256']
    left, right = vectors(serial), vectors(computed)
    error = max(abs(a-b) for row, other in zip(left,right,strict=True) for a,b in zip(row,other,strict=True))
    error /= max(1,max(abs(value) for row in left for value in row))
    assert error <= 1e-5, error
    return {'build':{**udf_receipt(build),'publication':publication['id'],'elf_sha256':hashlib.sha256(library).hexdigest()},
            'integrate':{**udf_receipt(job),'publication':integrated['id'],'relative_velocity_error':error},
            'marker':marker}, source, computed


async def cancel_udf(client, api, peer, owner, worker, solver, scratch, control, previous):
    token, case = owner
    extended, count = re.subn(r'(endTime\s+)0\.5;',r'\g<1>300;',control)
    assert count==1
    await put(client, api, owner, CONTROL_DICT, extended)
    submitted = await request(client, api, 'POST', f'/cases/{case}/udf:integrate', token=token,
                              json={'name':UDF_NAME,'run_dir':RUN})
    route = f'/jobs/{submitted["job_id"]}'
    async with asyncio.timeout(60):
        while True:
            job = await request(client, peer, 'GET', route, token=token)
            if job['ended_at'] is not None:
                (scratch/'unexpected-udf-stop.json').write_text(json.dumps(job,indent=2),encoding='utf-8')
                raise AssertionError(('UDF stopped before cancellation',job['id'],job['status']))
            children = job['attempt']['child_processes']['children'].values()
            running = next((child for child in children if child['phase']=='running'), None)
            if running is not None:
                parent = str(running['identity']['pid'])
                observed = await asyncio.to_thread(docker,'exec',worker,'python','-B','-c',OBSERVE_CHILD,parent,solver,'argv0')
                if observed:
                    break
            await asyncio.sleep(.2)
    assert (await request(client, peer, 'POST', route+':cancel', token=token))['ok']
    job = await finished(client, peer, token, submitted['job_id'], scratch, expected='cancelled')
    assert docker('exec',worker,'python','-B','-c',OBSERVE_CHILD,parent,solver,'argv0')==''
    assert job['attempt']['outputs'].get('publication',{}).get('status')!='committed'
    assert await fetch(client, peer, owner, FIELD)==previous
    return {**udf_receipt(job),'observed_solver':[int(pid) for pid in observed.splitlines()],
            'previous_field_preserved':True}


async def verify(api, peer, worker, redis, solver, scratch, evidence):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as client:
        command_owner = await account(client, api)
        udf_owner = await account(client, peer)
        evidence['commands'] = await command_jobs(client,api,peer,command_owner,redis,scratch)
        evidence['udf'], control, previous = await run_udf(client,api,peer,udf_owner,scratch)
        evidence['cancel'] = await cancel_udf(client,api,peer,udf_owner,worker,solver,scratch,control,previous)
        await request(client,peer,'GET',f'/jobs/{evidence["udf"]["integrate"]["job"]}',token=command_owner[0],expected=404)


def main(args):
    scratch = Path(tempfile.mkdtemp(prefix='j16-command-udf-',dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('COMMAND_UDF_ISOLATION_EVIDENCE',scratch,flush=True)
    original = set(docker('ps','-q','--no-trunc').splitlines())
    evidence = {'passed':False,'storage':args.storage,'actual_model_calls':0,'simulated_model_calls':0}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack,scratch,args.api_image,WEB_IMAGE)
            docker('stop',deployment.worker)
            if args.storage=='s3':
                deployment.configure_s3(scratch)
            docker('run','--rm','--network','none','--mount',deployment.data,'--entrypoint','/bin/sh',args.api_image,
                   '-ec',f'install -d -o 65534 -g 65534 /work/data/files; touch {CANARY}')
            deployment.env[ENV] = 'fixture present'
            deployment.flags.extend(('-e',ENV))
            deployment.worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec',deployment.worker,'id','-u')=='65534'
            solver = docker('exec',deployment.worker,'/bin/bash','-c',
                            'source /usr/lib/openfoam/openfoam2406/etc/bashrc && command -v icoFoam').splitlines()[-1]
            redis = docker('inspect','-f','{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',deployment.redis)
            docker('exec',deployment.worker,'python','-I','-c',
                   'import os,socket,sys; assert os.path.isfile(sys.argv[1]); assert os.environ[sys.argv[2]]; '
                   'socket.create_connection((sys.argv[3],6379),timeout=2).close()',CANARY,ENV,redis)
            _, api = deployment.api(deployment.a)
            if args.storage=='s3':
                cache = deployment.volume('cold-peer-cache','/work/data/files')
                docker('run','--rm','--network','none','--mount',cache,'--entrypoint','/usr/bin/chown',
                       args.api_image,'65534:65534','/work/data/files')
                deployment.runtime.extend(('--mount',cache))
            reader, peer = deployment.api(deployment.b)
            if args.storage=='s3':
                assert docker('exec',reader,'find','/work/data/files','-type','f')==''
            asyncio.run(verify(api,peer,deployment.worker,redis,solver,scratch,evidence))
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:1200]
        raise
    finally:
        evidence['original_services'] = original==set(docker('ps','-q','--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (scratch/'result.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        print('COMMAND_UDF_ISOLATION_RESULT',evidence['passed'],flush=True)
    assert evidence['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage',choices=('fs','s3'),required=True)
    parser.add_argument('--api-image',default=IMAGE)
    main(parser.parse_args())
