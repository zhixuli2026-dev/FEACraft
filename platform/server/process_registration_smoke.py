"""Durable launch gates and read-only identity through disposable real APIs."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic,engine_for


CONTROLLED='''import asyncio,copy,json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import SessionLocal
from app.models import JobAttempt
from app.services import job_execution
root=Path(__file__).parent
original=job_execution.phase
async def phase(owner,target,*,identity=None):
    if target!='running': return await original(owner,target,identity=identity)
    mode=(root/'mode').read_text() if (root/'mode').exists() else ''
    (root/'launch.json').write_text(json.dumps(identity))
    (root/'job-id').write_text(str(owner.job_id))
    if mode=='cancel_before_registration': raise asyncio.CancelledError
    if mode=='hold':
        (root/'registration.ready').touch()
        for _ in range(1000):
            if (root/'registration.release').exists(): break
            await asyncio.sleep(.02)
        else: raise TimeoutError('Test registration barrier expired')
    if mode=='fail_commit':
        async def fail(_): raise OSError('Injected registration commit failure')
        with patch.object(AsyncSession,'commit',fail):
            return await original(owner,target,identity=identity)
    if mode.startswith('bad_'):
        identity=copy.deepcopy(identity)
        if mode=='bad_birth': identity['host']['creation_time_100ns']=str(int(identity['host']['creation_time_100ns'])+1)
        if mode=='bad_host': identity['host']['host_id']='0'*64
        if mode=='bad_hash': identity['gate_sha256']='0'*64
        if mode=='bad_distribution': identity['native']['distribution']='vibeflow-test-nonexistent-distribution'
    result=await original(owner,target,identity=identity)
    if mode=='cancel_after_registration': raise asyncio.CancelledError
    if mode=='duplicate':
        async with SessionLocal() as db:
            before=(await db.get(JobAttempt,owner.attempt_id)).version
        assert await original(owner,target,identity=identity)=='running'
        changed=copy.deepcopy(identity)
        changed['launch_nonce']='a'*64
        changed['scope']='Local\\\\VibeFlow-'+changed['launch_nonce']
        for fake_owner,fake_record,worker in ((owner,changed,None),(replace(owner,token='forged'),identity,None),(owner,identity,'foreign-worker')):
            try:
                with patch.object(job_execution,'WORKER_ID',worker or job_execution.WORKER_ID):
                    await original(fake_owner,target,identity=fake_record)
            except job_execution.ExecutionConflict: pass
            else: raise AssertionError('Conflicting process registration accepted')
        async with SessionLocal() as db:
            assert (await db.get(JobAttempt,owner.attempt_id)).version==before
        (root/'duplicates.checked').touch()
    if mode=='registered_hold':
        (root/'registration.ready').touch()
        for _ in range(1000):
            if (root/'registration.release').exists(): break
            await asyncio.sleep(.02)
        else: raise TimeoutError('Test registered barrier expired')
    return result
job_execution.phase=phase
from app.main import app
'''


def wait_file(path):
    for _ in range(600):
        if path.exists(): return
        time.sleep(.025)
    raise TimeoutError(str(path))


async def replace_record(url,job_id,record):
    from sqlalchemy import update
    from app.models import JobAttempt
    engine=engine_for(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(update(JobAttempt).where(JobAttempt.job_id==job_id)
                .values(process_identity_json=json.dumps(record)))
    finally:
        await engine.dispose()


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    server=IsolatedServer('process-registration',app='controlled_process_registration:app',database_url=database_url)
    (server.root/'controlled_process_registration.py').write_text(CONTROLLED,encoding='utf-8')
    with server as api:
        api.create_case('Process registration')
        case_root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        marker=case_root/'payload.txt'
        def mode(value):
            (api.root/'mode').write_text(value)
            for path in ('registration.ready','registration.release','job-id','launch.json'):
                (api.root/path).unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
        def submit(runtime='windows',expect=200,command=None):
            return api.call('POST',api.prefix+'/jobs',{'type':'shell','runtime':runtime,
                'command':command or ('echo payload>payload.txt' if runtime=='windows' else 'printf payload > payload.txt'),
                'timeout_seconds':30,'max_log_bytes':8192},expect=expect,raw=expect==500)
        for state in ('hold','registered_hold'):
            mode(state)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(submit)
                try:
                    wait_file(api.root/'registration.ready')
                    job_id=int((api.root/'job-id').read_text())
                    current=api.call('GET',f'/jobs/{job_id}')
                    observation=api.call('GET',f'/jobs/{job_id}/process')
                    assert not marker.exists()
                    if state=='hold':
                        assert current['attempt']['phase']=='starting'
                        assert current['attempt']['process_identity'] is None
                        assert observation['state']=='unverified'
                    else:
                        assert current['attempt']['phase']=='running'
                        assert observation['state']=='match'
                        assert observation['identity']==current['attempt']['process_identity']
                finally:
                    (api.root/'registration.release').touch()
                row=future.result(timeout=20)
            api.wait_job(row['id'])
            assert marker.read_text().strip()=='payload'
            ended=api.call('GET',f'/jobs/{row["id"]}/process')
            assert ended['state']=='exited' and ended['identity']['job_id']==row['id']
            done(state+'_does_not_release_payload_before_registration_returns')

        mode('duplicate')
        row=submit()
        api.wait_job(row['id'])
        assert (api.root/'duplicates.checked').exists()
        done('identical_registration_is_idempotent_and_changed_nonce_token_worker_are_rejected')

        for state in ('fail_commit','bad_birth','bad_host','bad_hash','bad_distribution'):
            mode(state)
            submit('wsl' if state=='bad_distribution' else 'windows',expect=409)
            job_id=int((api.root/'job-id').read_text())
            row=api.wait_job(job_id,expected='failed')
            assert row['attempt']['process_identity'] and row['attempt']['process_stop']['verified'] and not marker.exists()
            from app.services.process_identity_native import probe
            launch=json.loads((api.root/'launch.json').read_text())
            assert all(row['attempt']['process_identity'][key]==value for key,value in launch.items())
            assert row['attempt']['registration']['stage']=='aborted'
            assert probe(launch['host'])['state']=='exited'
            if launch['native']:
                from app.services.owned_process import probe_native
                assert asyncio.run(probe_native(launch['native']['distribution'],launch['native']['process']))['state']=='exited'
            done(state+'_cannot_release_payload_and_owned_helpers_exit')

        for state in ('cancel_before_registration','cancel_after_registration'):
            mode(state)
            submit(expect=500)
            job_id=int((api.root/'job-id').read_text())
            current=api.wait_job(job_id,expected='cancelled')
            assert not marker.exists()
            assert current['attempt']['process_identity'] and current['attempt']['process_stop']['verified']
            assert current['attempt']['registration']['stage']=='aborted'
            launch=json.loads((api.root/'launch.json').read_text())
            assert probe(launch['host'])['state']=='exited'
            done(state+'_settles_owned_attempt_without_running_payload')

        for runtime in ('windows','wsl','wsl-sandbox'):
            mode('')
            row=submit(runtime)
            final=api.wait_job(row['id'])
            record=final['attempt']['process_identity']
            assert record['runtime']==runtime and record['case_id']==api.case_id
            assert bool(record['native'])==(runtime!='windows')
            assert api.call('GET',f'/jobs/{row["id"]}/process')['state']=='exited'
            assert marker.read_text().strip()=='payload'
            done(runtime+'_real_shell_has_bound_durable_birth_identity_and_exit')

        mode('')
        row=submit('windows',command='echo FAST_EXIT && exit /b 7')
        final=api.wait_job(row['id'],expected='failed')
        record=final['attempt']['process_identity']
        cleanup=final['attempt']['process_stop']
        assert cleanup and cleanup['verified']
        assert record and final['exit_code']==7
        assert 'FAST_EXIT' in final['log_backlog']
        done('fast_failed_shell_preserves_identity_log_and_native_exit_code')

        # A second API and its replacement read the committed record, not the
        # first process's registry. Each reader uses a disposable port.
        peer=IsolatedServer('process-registration-reader')
        peer.token=api.token
        for generation in range(2):
            log=(peer.root/f'api-{generation}.log').open('w',encoding='utf-8')
            process=subprocess.Popen([sys.executable,'-B','-m','uvicorn','app.main:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],cwd=peer.root,env=api.env,
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                for _ in range(200):
                    assert process.poll() is None
                    try:
                        if peer.call('GET','/api/health',api_prefix=False).get('ok'): break
                    except OSError: pass
                    time.sleep(.05)
                else: raise TimeoutError('Reader API did not start')
                assert peer.call('GET',f'/jobs/{row["id"]}')['attempt']['process_identity']==record
                assert peer.call('GET',f'/jobs/{row["id"]}')['attempt']['process_stop']==cleanup
                assert peer.call('GET',f'/jobs/{row["id"]}/process')['state']=='exited'
            finally:
                process.terminate()
                process.wait(timeout=10)
                log.close()
        done('two_API_reader_generations_recover_identical_committed_identity_and_cleanup')

        for name,mutation,expected in (
                ('legacy',{'pid':record['pid'],'runtime':'windows'},'unverified'),
                ('wrong_attempt',{**record,'attempt_id':str(uuid.uuid4())},'unverified'),
                ('foreign_host',{**record,'host':{**record['host'],'host_id':'0'*64}},'mismatch'),
                ('extra_secret',{**record,'claim_token':'MUST_NOT_LEAK'},'unverified')):
            asyncio.run(replace_record(api.env['VIBEFLOW_DATABASE_URL'],row['id'],mutation))
            observation=api.call('GET',f'/jobs/{row["id"]}/process')
            assert observation['state']==expected and 'MUST_NOT_LEAK' not in json.dumps(observation)
            if expected=='unverified':
                assert observation['identity'] is None
                assert api.call('GET',f'/jobs/{row["id"]}')['attempt']['process_identity'] is None
            done(name+'_record_is_never_treated_as_matching_identity')
        asyncio.run(replace_record(api.env['VIBEFLOW_DATABASE_URL'],row['id'],record))
        owner_token=api.token
        api.create_case('Other process reader')
        api.call('GET',f'/jobs/{row["id"]}/process',expect=404)
        api.token=owner_token
        done('process_identity_API_enforces_case_ownership')
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'process-registration-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='process-registration-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_process_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_process_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('PROCESS_REGISTRATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
