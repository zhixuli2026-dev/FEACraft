"""Exercise real ECXML conversion and compiled FloTHERM helpers in Linux workspaces."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shlex
import tempfile

import aiohttp

from flotherm_two_r_acceptance import CONFIG, ECXML
from linux_command_udf_isolation_acceptance import put
from linux_geometry_isolation_acceptance import OBSERVE_CHILD, receipt
from linux_openfoam_isolation_acceptance import IMAGE, finished, namespace
from linux_solver_isolation_acceptance import fetch
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import WEB_IMAGE, Deployment, docker


INPUT = 'Ref/compact.ecxml'
SETTINGS = 'Ref/compact-settings.json'
MODEL = 'Ref/compact-ir.json'
OUTPUT = 'C-run/compact-thermal'
SOURCE = OUTPUT+'/constant/flotherm-two-r-src'
DESTINATION = 'UDF/compact-two-r'
LIBRARY = DESTINATION+'/libvibeflowFlothermTwoR.so'
BASHRC = '/usr/lib/openfoam/openfoam2406/etc/bashrc'
CANARY = '/work/data/flotherm-host-fixture'
ENV = 'FLOTHERM_ACCEPTANCE_FIXTURE'


async def publication_manifest(client, peer, owner, job):
    record = job['attempt']['outputs']['publication']
    assert record['status'] == 'committed'
    base = f'.vibeflow/publications/{record["id"]}'
    return record['id'], base, json.loads(await fetch(client, peer, owner, base+'/manifest.json'))


async def conversion(client, api, peer, owner, scratch):
    token, case = owner
    route = f'/cases/{case}/flotherm'
    await put(client, api, owner, INPUT, ECXML)
    await put(client, api, owner, SETTINGS, json.dumps(CONFIG))
    inspected = await request(client, api, 'POST', route+':inspect', token=token, json={'paths':[INPUT]})
    digest = hashlib.sha256(ECXML.encode('utf-8')).hexdigest()
    assert digest in json.dumps(inspected)
    imported = await request(client, api, 'POST', route+':import', token=token, json={
        'path': INPUT, 'settings_path': SETTINGS, 'expected_sha256': digest})
    await put(client, api, owner, MODEL, imported['content'])
    started = await request(client, api, 'POST', route+':convert', token=token,
                            json={'ir_path':MODEL,'output_dir':OUTPUT})
    job = await finished(client, peer, token, started['job_id'], scratch)
    publication, base, manifest = await publication_manifest(client, peer, owner, job)
    for name in ('case-manifest.json','preflight.json','constant/flotherm-two-r-src/Make/files'):
        path = OUTPUT+'/'+name
        archive = await fetch(client, peer, owner, base+'/files/'+path)
        assert archive == await fetch(client, peer, owner, path)
        assert hashlib.sha256(archive).hexdigest() == manifest['files'][path]['sha256']
        (scratch/Path(name).name).write_bytes(archive)
    converted = json.loads((scratch/'case-manifest.json').read_text('utf-8'))
    preflight = json.loads((scratch/'preflight.json').read_text('utf-8'))
    network, = converted['two_resistance_networks']
    assert preflight['ok']
    assert (network['theta_jc_k_w'], network['theta_jb_k_w'], network['power_w']) == (25,45,.3)
    assert await fetch(client, peer, owner, INPUT) == ECXML.encode('utf-8')
    return {**receipt(job),'publication':publication,'output_files':len(manifest['files']),
            'input_sha256':digest,'network_model':network,'preflight':True}


def compiler_receipt(job):
    native, = job['attempt']['child_processes']['children'].values()
    assert native['purpose']=='flotherm_library_linux'
    assert native['stop']['verified'] and native['stop']['scope_empty']
    return {'job':job['id'],'network':namespace(native['identity']),
            'root_stop':job['attempt']['process_stop'],'child_stop':native['stop']}


async def library(client, api, peer, owner, scratch):
    token, case = owner
    route = f'/cases/{case}/flotherm:build-library'
    original = await fetch(client,peer,owner,SOURCE+'/twoResistanceNetworkSource.C')
    response = await request(client,api,'POST',route,token=token,
                             json={'source_dir':SOURCE,'destination_dir':DESTINATION})
    job = await finished(client,peer,token,response['job_id'],scratch)
    publication, base, manifest = await publication_manifest(client, peer, owner, job)
    native = manifest['descriptor']['receipt']['native']
    assert native['distro']=='native-linux' and set(native['steps'])=={'build','load'}
    assert all(step['returncode']==0 for step in native['steps'].values())
    data = await fetch(client,peer,owner,base+'/files/'+LIBRARY)
    digest = hashlib.sha256(data).hexdigest()
    assert data.startswith(b'\x7fELF') and digest==native['libraries'][LIBRARY]['sha256']
    assert data==await fetch(client,peer,owner,LIBRARY)
    assert b'FLOTHERM_LIBRARY_LOADED' in await fetch(client,peer,owner,DESTINATION+'/flotherm-load.log')
    assert await fetch(client,peer,owner,SOURCE+'/twoResistanceNetworkSource.C')==original
    return {**compiler_receipt(job),'publication':publication,'sha256':digest}, data


async def consume_library(client, api, peer, owner, publication, ipc, scratch):
    token, case = owner
    code = ('import ctypes,os,sys; from pathlib import Path; '
            'assert os.readlink("/proc/self/ns/ipc")!=sys.argv[2]; '
            'assert not Path(sys.argv[3]).exists() and sys.argv[4] not in os.environ; '
            'lib=Path(os.environ["FOAM_LIBBIN"]); '
            '[ctypes.CDLL(str(lib/name),mode=ctypes.RTLD_GLOBAL) for name in '
            '("libOpenFOAM.so","libfiniteVolume.so","libmeshTools.so","libfileFormats.so",'
            '"libsurfMesh.so","sys-openmpi/libPstream.so")]; '
            'ctypes.CDLL(str(Path(sys.argv[1]).resolve()),mode=ctypes.RTLD_GLOBAL); '
            'print("FROZEN_FLOTHERM_LIBRARY_LOADED",os.readlink("/proc/self/ns/ipc"))')
    command = shlex.join(['/bin/bash','--noprofile','--norc','-c','source '+BASHRC+' && exec '+shlex.join(
        ['python3','-I','-c',code,LIBRARY,ipc,CANARY,ENV])])
    response = await request(client,api,'POST',f'/cases/{case}/jobs',token=token,json={
        'command': command,
        'input_publications':[{'publication_id':publication,'paths':[LIBRARY],'require_current':True}]})
    job = await finished(client,peer,token,response['id'],scratch)
    row, = [line for line in job['log_backlog'].splitlines() if line.startswith('FROZEN_FLOTHERM_LIBRARY_LOADED ')]
    private_ipc = row.split()[-1]
    assert private_ipc.startswith('ipc:[') and private_ipc!=ipc
    return {**receipt(job),'host_ipc':ipc,'isolated_ipc':private_ipc,'library_loaded':True}


async def cancel_library(client, api, peer, owner, worker, compiler, old, original, scratch):
    token, case = owner
    destination = 'UDF/compact-two-r-interrupted'
    response = await request(client,api,'POST',f'/cases/{case}/flotherm:build-library',token=token,
                             json={'source_dir':SOURCE,'destination_dir':destination})
    route = f'/jobs/{response["job_id"]}'
    async with asyncio.timeout(65):
        while True:
            job = await request(client,peer,'GET',route,token=token)
            if job['ended_at'] is not None:
                raise AssertionError(('Library stopped before cancellation',job['status'],job['log_backlog'][-1500:]))
            children = job['attempt']['child_processes']['children'].values()
            child = next((item for item in children if item['phase']=='running'), None)
            if child is not None:
                parent = str(child['identity']['pid'])
                observed = await asyncio.to_thread(docker,'exec',worker,'python','-B','-c',OBSERVE_CHILD,parent,compiler,'argv0')
                if observed:
                    break
            await asyncio.sleep(.15)
    assert (await request(client,peer,'POST',route+':cancel',token=token))['ok']
    job = await finished(client,peer,token,response['job_id'],scratch,expected='cancelled')
    assert docker('exec',worker,'python','-B','-c',OBSERVE_CHILD,parent,compiler,'argv0')==''
    assert job['attempt']['outputs'].get('publication',{}).get('status')!='committed'
    assert await fetch(client,peer,owner,LIBRARY)==original
    archived = f'.vibeflow/publications/{old["publication"]}/files/'+LIBRARY
    assert await fetch(client,peer,owner,archived)==original
    return {**compiler_receipt(job),'observed_cc1plus':[int(pid) for pid in observed.splitlines()],
            'previous_library_preserved':True}


async def verify(api, peer, worker, compiler, ipc, evidence, scratch):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=95)) as client:
        owner = await account(client,api)
        other = await account(client,peer)
        evidence['conversion'] = await conversion(client,api,peer,owner,scratch)
        evidence['library'], data = await library(client,api,peer,owner,scratch)
        evidence['use'] = await consume_library(client,api,peer,owner,evidence['library']['publication'],ipc,scratch)
        evidence['cancel'] = await cancel_library(client,api,peer,owner,worker,compiler,evidence['library'],data,scratch)
        await request(client,peer,'GET',f'/jobs/{evidence["library"]["job"]}',token=other[0],expected=404)


def main(args):
    scratch = Path(tempfile.mkdtemp(prefix='j16-flotherm-',dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('FLOTHERM_ISOLATION_EVIDENCE',scratch,flush=True)
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
            deployment.env[ENV] = 'parent fixture'
            deployment.flags.extend(('-e',ENV))
            deployment.worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec',deployment.worker,'id','-u')=='65534'
            docker('exec',deployment.worker,'python','-I','-c',
                   'import os,sys; assert os.path.isfile(sys.argv[1]); assert os.environ[sys.argv[2]]',CANARY,ENV)
            compiler = docker('exec',deployment.worker,'g++','-print-prog-name=cc1plus')
            ipc = docker('exec',deployment.worker,'readlink','/proc/1/ns/ipc')
            assert compiler.startswith('/') and ipc.startswith('ipc:[')
            _, api = deployment.api(deployment.a)
            if args.storage=='s3':
                cache = deployment.volume('cold-peer-cache','/work/data/files')
                docker('run','--rm','--network','none','--mount',cache,'--entrypoint','/usr/bin/chown',
                       args.api_image,'65534:65534','/work/data/files')
                deployment.runtime.extend(('--mount',cache))
            reader, peer = deployment.api(deployment.b)
            if args.storage=='s3':
                assert docker('exec',reader,'find','/work/data/files','-type','f')==''
            asyncio.run(verify(api,peer,deployment.worker,compiler,ipc,evidence,scratch))
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:1200]
        raise
    finally:
        evidence['original_services'] = original==set(docker('ps','-q','--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (scratch/'result.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        print('FLOTHERM_ISOLATION_RESULT',evidence['passed'],flush=True)
    assert evidence['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage',choices=('fs','s3'),required=True)
    parser.add_argument('--api-image',default=IMAGE)
    main(parser.parse_args())
