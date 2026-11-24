"""Real OpenSSH/SFTP, Slurm or OpenPBS, native OpenFOAM and durable case archives."""

import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import asyncssh

from app.services.process_identity_native import probe
from live_llm_terminal_agent_acceptance import disposable_database
from live_llm_shared_index_acceptance import SharedIndexServer
from s3testsupport import ObjectStore
from testsupport import SERVER


ROOT = SERVER.parents[1]
IMAGE = 'sha256:346beedd6f1c9d74d535ace91b6cb75fbd23e6243138672453c5a705ae789ee1'
REFERENCE = ROOT/'tmp/FF-CB07-conformal-pressure-20261123/independent'
BOOTSTRAP = ROOT/'tmp/FF-C03-local-scheduler-20261123/start.sh'
BOOTSTRAP_SHA256 = '3e81b667f65b1730e484bc8d5b26170f8065b3c8171f45f05b852828954d17eb'
INPUTS = ('0/U','0/p','system/blockMeshDict','system/controlDict','system/fvSchemes','system/fvSolution',
          'constant/transportProperties')
SOLVE = '''#!/bin/bash
source /usr/lib/openfoam/openfoam2406/etc/bashrc || exit 1
set -euo pipefail
printf 'allocation=%s uid=%s\n' "${SLURM_JOB_ID:-${PBS_JOBID:?}}" "$(id -u)"
blockMesh > blockMesh.log
checkMesh -allTopology -allGeometry > checkMesh.log
grep '^Mesh OK' checkMesh.log
icoFoam > solve.log
'''
RESUME = '''#!/bin/bash
source /usr/lib/openfoam/openfoam2406/etc/bashrc || exit 1
set -euo pipefail
blockMesh > blockMesh.log
foamDictionary system/controlDict -entry startFrom -set latestTime
foamDictionary system/controlDict -entry endTime -set 0.04
icoFoam > resume.log
'''
PROCESS = '''
from app.services.process_identity_native import observe
@app.get('/fixture/process')
async def process_birth():
    return observe(os.getpid())
'''


def docker(*args,check=True):
    result = subprocess.run(['docker',*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        timeout=45,check=check,encoding='utf8',errors='replace')
    return result.stdout.strip()


def wait(callback, seconds=50):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        if value := callback():
            return value
        time.sleep(.2)
    raise TimeoutError('The expected HPC state was not observed')


def configure(api, env, *, dist=None):
    static = '' if dist is None else ('\nfrom fastapi.staticfiles import StaticFiles\n'
                                      f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n')
    (api.root/'hpc_api.py').write_text('import os\nos.environ.update('+repr(env)+')\nfrom app.main import app\n'+PROCESS,
                                    encoding='utf8')
    if static:
        with (api.root/'hpc_api.py').open('a',encoding='utf8') as module:
            module.write(static)


def scenario(database, *, s3, browser_python=None, pbs_fixture=None):
    provider = 'pbs' if pbs_fixture else 'slurm'
    profile_name = 'local-'+provider
    api = SharedIndexServer('ff-c16-'+provider+'-primary',app='hpc_api:app',database_url=database)
    peer = SharedIndexServer('ff-c16-'+provider+'-peer',app='hpc_api:app',database_url=database)
    root = api.root
    outcome = {'passed':False,'scheduler':provider,'physical_hosts':1,'mock_llm_calls':0,'actual_model_calls':0,'checks':[]}
    print('C16_EVIDENCE '+str(root),flush=True)
    evidence = root/'scheduler.json'

    def record(check, **fields):
        outcome['checks'].append(check)
        outcome.update(fields)
        evidence.write_text(json.dumps(outcome,indent=2),encoding='utf8')
        print('C16_PASS '+check,flush=True)

    known = root/'known_hosts'
    label = 'FF-C16-PBS' if pbs_fixture else 'FF-C16'
    started = False
    identity = None
    store = ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed', volume_max=6,
                        authenticated=True) if s3 else nullcontext(None)
    try:
        if pbs_fixture:
            fixture = pbs_fixture.resolve(strict=True)
            assert fixture.is_relative_to((ROOT/'tmp').resolve())
            container = json.loads((fixture/'container.json').read_text(encoding='utf-8-sig'))['name']
            assert re.fullmatch(r'vf-c16-pbs-[a-f0-9]{12}',container)
            assert docker('inspect',container,'--format','{{index .Config.Labels "org.vibeflow.acceptance"}}') == label
            identity = fixture/'client.key'
        else:
            bootstrap = root/'bootstrap'
            bootstrap.mkdir()
            setup = BOOTSTRAP.read_bytes()
            assert hashlib.sha256(setup).hexdigest() == BOOTSTRAP_SHA256
            (bootstrap/'start.sh').write_bytes(setup)
            key = asyncssh.generate_private_key('ssh-ed25519')
            (bootstrap/'client.pub').write_bytes(key.export_public_key())
            identity = root/'client.key'
            identity.write_bytes(key.export_private_key())
            container = 'vf-c16-slurm-' + uuid4().hex[:12]
            docker('run','-d','--init','--name',container,'--label','org.vibeflow.acceptance='+label,
                   '--hostname','vibeflow-slurm','--publish','127.0.0.1::22',
                   '--mount',f'type=bind,source={bootstrap},target=/bootstrap,readonly',
                   '--entrypoint','/bin/bash',IMAGE,'/bootstrap/start.sh')
        started = True
        port = int(docker('port',container,'22/tcp').rsplit(':',1)[-1])
        def host_key():
            value = docker('exec',container,'cat','/run/sshd/host_key.pub',check=False)
            return value if value.startswith('ssh-ed25519 ') else None
        host = wait(host_key)
        known.write_text(f'[127.0.0.1]:{port} {host.split()[0]} {host.split()[1]}\n',encoding='ascii')
        if pbs_fixture:
            wait(lambda:'state = free' in docker('exec',container,'/opt/pbs/bin/pbsnodes','-a',check=False))
            outcome['scheduler_version']=docker('exec',container,'/opt/pbs/bin/qstat','--version')
        else:
            wait(lambda:'idle' in docker('exec',container,'sinfo','--noheader','--format','%T',check=False))
            docker('exec','--user','vfaccept',container,'mkdir','-m','700','/home/vfaccept/vibeflow-hpc')
            outcome['scheduler_version']=docker('exec',container,'sinfo','--version')
        queue = 'workq' if pbs_fixture else 'local'
        profile = {'org_id':1,'scheduler':provider,'queue':queue,'root':'/home/vfaccept/vibeflow-hpc',
                   'host':'127.0.0.1','port':port,'username':'vfaccept','client_key':str(identity),
                   'known_hosts':str(known),'max_seconds':600}
        config = {'VIBEFLOW_REMOTE_HOSTS':'127.0.0.1','VIBEFLOW_REMOTE_HPC_PROFILES':json.dumps({profile_name:profile})}
        with store as object_store:
            if object_store:
                config.update(object_store.settings())
                outcome['object_store']=str(object_store.root)
            configure(api,config)
            with api:
                api.create_case('Actual OpenPBS OpenFOAM' if pbs_fixture else 'Actual Slurm OpenFOAM')
                profiles = api.call('GET','/remote/profiles')
                assert profiles==[{'name':profile_name,'scheduler':provider,'queue':queue,'max_seconds':600}],profiles
                endpoint = api.prefix+'/remote-jobs'
                for path in INPUTS:
                    api.put(path,(REFERENCE/path).read_text(encoding='utf8'))
                api.put('run.sh',SOLVE)
                working = {'profile':profile_name,'script':'run.sh','input_paths':['run.sh',*INPUTS],
                           'output_paths':['0.02/U','0.02/p','checkMesh.log','solve.log'],'timeout_seconds':180}
                header = {'Idempotency-Key':'slurm-openfoam-fixture'}
                run = api.call('POST',endpoint,working,expect=202,extra_headers=header)
                assert run['scheduler_id'] is not None,run
                duplicate = api.call('POST',endpoint,working,expect=202,extra_headers=header)
                assert duplicate['id']==run['id'] and duplicate['scheduler_id']==run['scheduler_id']
                api.call('POST',endpoint,{**working,'timeout_seconds':181},expect=409,extra_headers=header)
                record('real scheduler submission and idempotent acknowledgement', original=run)
                bind = {**config,'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET'],
                        'VIBEFLOW_STORAGE_FS_ROOT':str(api.root/'files')}
                if database is None:
                    bind['VIBEFLOW_DATABASE_URL'] = api.env['VIBEFLOW_DATABASE_URL']
                configure(peer,bind,dist=(SERVER.parent/'web/dist').resolve(strict=True) if browser_python else None)
                peer.token,peer.case_id,peer.prefix = api.token,api.case_id,api.prefix
            assert wait(lambda:probe(api.birth)['state']=='exited')

            with peer:
                def refresh(run_id, desired, *, seconds=50):
                    def check():
                        view = peer.call('POST',endpoint+'/'+run_id+':refresh')
                        if view['state'] in desired:
                            return view
                        assert view['state'] not in {'failed','cancelled','succeeded'},view
                        return None
                    return wait(check,seconds)

                durable = peer.call('GET',endpoint+'/'+run['id'])
                assert durable['scheduler_id']==run['scheduler_id']
                done = refresh(run['id'],{'succeeded'})
                assert done['observation']['scheduler_state']==('F' if pbs_fixture else 'COMPLETED')
                assert done['observation']['exit_code']==0
                raw = endpoint+'/'+run['id']+'/files?path='
                hashes = {}
                for path in ['0.02/U','0.02/p']:
                    value = peer.call('GET',raw+path,raw=True)
                    assert value==(REFERENCE/path).read_bytes(),path
                    hashes[path] = hashlib.sha256(value).hexdigest()
                logs = peer.call('GET',raw+'stdout.log',raw=True)
                assert b'uid=1000' in logs and b'Mesh OK' in logs
                assert peer.call('GET',raw+'solve.log',raw=True)
                if pbs_fixture:
                    native = docker('exec',container,'/opt/pbs/bin/qstat','-x','-f','-F','json',run['scheduler_id'])
                    job = json.loads(native)['Jobs'][run['scheduler_id']]
                    assert job['job_state']=='F' and job['Exit_status']==0
                else:
                    native = docker('exec',container,'scontrol','-o','show','job',run['scheduler_id'])
                    assert 'JobState=COMPLETED' in native and 'ExitCode=0:0' in native
                record('cold API reads scheduler receipt and collects original OpenFOAM fields', field_sha256=hashes)

                peer.put('resume.sh',RESUME)
                resume = peer.call('POST',endpoint,{'profile':profile_name,'script':'resume.sh','input_paths':['resume.sh',*INPUTS],
                    'output_paths':['0.04/U','0.04/p','resume.log'],'timeout_seconds':60,'restart':{'run_id':run['id'],
                        'files':[{'output_path':'0.02/U','input_path':'0.02/U'},
                                 {'output_path':'0.02/p','input_path':'0.02/p'}]}},expect=202,
                    extra_headers={'Idempotency-Key':'slurm-resume-fixture'})
                resumed = refresh(resume['id'],{'succeeded'})
                assert resumed['source_run_id']==run['id'] and resumed['scheduler_id']!=run['scheduler_id']
                assert resumed['input_files']['0.02/U']['sha256']==hashes['0.02/U']
                assert resumed['input_files']['0.02/p']['sha256']==hashes['0.02/p']
                resumed_files = endpoint+'/'+resume['id']+'/files?path='
                log = peer.call('GET',resumed_files+'resume.log',raw=True)
                (root/'resume.log').write_bytes(log)
                times = [line.decode('ascii') for line in log.splitlines() if line.startswith(b'Time = ')]
                assert times == ['Time = 0.03','Time = 0.04'],times
                assert b'location    "0.04"' in peer.call('GET',resumed_files+'0.04/U',raw=True)
                record('explicit continuation stages the exact prior checkpoints and solves two further time steps',
                       resumed=resumed, resumed_times=times)

                peer.put('long.sh','#!/bin/bash\necho RUNNING\nexec sleep 90\n')
                pending = peer.call('POST',endpoint,{'profile':profile_name,'script':'long.sh','input_paths':['long.sh'],
                    'timeout_seconds':180},expect=202,extra_headers={'Idempotency-Key':'slurm-cancel-fixture'})
                refresh(pending['id'],{'running'})
                during = peer.call('GET','/org/mine')['usage']
                assert during['running_jobs']==1 and during['occupied_job_slots']==1
                stopping = peer.call('POST',endpoint+'/'+pending['id']+':cancel')
                assert stopping['state'] in {'cancelling','cancelled'}
                stopped = refresh(pending['id'],{'cancelled'})
                assert stopped['observation']['cancel_requested']
                assert stopped['observation']['scheduler_state']==('F' if pbs_fixture else 'CANCELLED')
                after = peer.call('GET','/org/mine')['usage']
                assert after['running_jobs']==0 and after['occupied_job_slots']==0
                audit = peer.call('GET','/org/1/audit')
                actions = {event['action'] for event in audit['events'] if event['resource_id']==pending['id']}
                assert audit['chain_valid'] and actions=={'hpc.submit','hpc.cancel','hpc.completed'}
                record('terminal cancellation releases actual job capacity and closes its audit history', cancel=stopped)
                outsider = peer.call('POST','/auth/register',{'email':uuid4().hex+'@example.com',
                    'name':'Outsider','password':'isolated-slurm-123'})
                authorization = {'Authorization':'Bearer '+outsider['token']}
                assert peer.call('GET','/remote/profiles',extra_headers=authorization)==[]
                peer.call('GET',endpoint+'/'+run['id'],expect=404,extra_headers=authorization)
                assert len(peer.call('GET',endpoint))==3
                record('nonmember cannot discover the scheduler profile or another case job')
                if browser_python:
                    peer.put('browser.sh',"#!/bin/bash\nprintf 'VIBEFLOW_SCHEDULER_BROWSER\\n' > browser-result.txt\n")
                    browser = subprocess.run([str(browser_python),'-B','-X','utf8',str(SERVER/'hpc_browser_acceptance.py')],
                        input=json.dumps({'root':str(root),'base':peer.base,'case_id':peer.case_id,'token':peer.token}),
                        text=True,encoding='utf8',capture_output=True,timeout=200)
                    assert browser.returncode==0,browser.stderr[-1800:]
                    assert json.loads((root/'hpc-browser.json').read_text(encoding='utf8'))['passed']
                    record('actual Edge submission receipt recovery and download at desktop and 390px')
            assert wait(lambda:probe(peer.birth)['state']=='exited')
        outcome['passed'] = True
    finally:
        if started:
            (root/'scheduler-container.log').write_text(docker('logs',container,check=False),encoding='utf8')
            assert docker('inspect',container,'--format','{{index .Config.Labels "org.vibeflow.acceptance"}}')==label
            docker('rm','-f',container)
            outcome['isolated_container_removed']=not docker('ps','-aq','--filter','name=^/'+container+'$')
        if identity is not None:
            identity.unlink(missing_ok=True)
        evidence.write_text(json.dumps(outcome,indent=2),encoding='utf8')


async def main(args):
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(scenario,database,s3=args.s3,browser_python=args.browser_python,pbs_fixture=args.openpbs_fixture)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--browser-python',type=Path)
    parser.add_argument('--openpbs-fixture',type=Path)
    asyncio.run(main(parser.parse_args()))
