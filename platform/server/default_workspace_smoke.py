"""Ordinary Job/batch default isolation through real Windows and WSL processes."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import uuid

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

FIXTURE='''
from fastapi import Depends
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt
from app.routers.workspaces import get_owned_case
@app.post('/api/v1/fixture/legacy/{case_id}')
async def old_history(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 await get_owned_case(case_id,user,db)
 db.add(Job(case_id=case_id,type='shell',command='old terminal without stop history',status='succeeded',exit_code=0))
 await db.commit();return {'ok':True}
@app.post('/api/v1/fixture/retry/{attempt_id}')
async def retry(attempt_id:str,user=Depends(get_current_user),db=Depends(get_db)):
 from app.services import job_execution,job_delivery
 previous=await db.get(JobAttempt,attempt_id);job=await db.get(Job,previous.job_id)
 await get_owned_case(job.case_id,user,db)
 new,attempt=await job_execution.allocate_retry(db,attempt_id)
 await job_delivery.request(db,new,user_id=user.id);await db.commit()
 await job_delivery.deliver(new.id)
 return {'job_id':new.id,'attempt_id':attempt.id}
'''


def command(code):return '"'+sys.executable+'" -c "'+code+'"'


def verify(database_url=None):
    api=IsolatedServer('default-workspace',app='default_fixture:app',database_url=database_url)
    (api.root/'default_fixture.py').write_text(FIXTURE,encoding='utf8')
    checks=[]
    with api:
        api.create_case('default private attempts')
        api.put('Ref/input.txt','original')
        api.put('C-run/probe/0/U','initial field')
        api.put('C-run/probe/0.orig/U','user original')
        root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        jobs=[]
        for value in ('one','two'):
            code="from pathlib import Path; import time; time.sleep(2); print(Path('Ref/input.txt').read_text()); Path('result.txt').write_text('"+value+"'); Path('C-run/probe/0/U').write_text('private edit')"
            jobs.append(api.call('POST',api.prefix+'/jobs',{'command':command(code)}))
        api.put('Ref/input.txt','new live edit')
        details=[api.wait_job(j['id']) for j in jobs]
        manifests=[d['attempt']['workspace'] for d in details]
        assert manifests[0]['run_path']!=manifests[1]['run_path']
        for d,m,value in zip(details,manifests,('one','two')):
            assert m['source']['selection_mode']=='case'
            assert 'original' in d['log_backlog'] and 'new live edit' not in d['log_backlog']
            assert (root/m['run_path']/'result.txt').read_text()==value
            assert d['attempt']['outputs']['files']['result.txt']['size']==len(value)
            assert (root/m['inputs_path']/'C-run/probe/0/U').read_text()=='initial field'
            assert all(not p.startswith('.vibeflow/') for p in m['files'])
        assert not (root/'result.txt').exists()
        assert (root/'C-run/probe/0/U').read_text()=='initial field'
        assert (root/'C-run/probe/0.orig/U').read_text()=='user original'
        checks.append('concurrent_default_jobs_keep_live_files_initial_fields_and_originals')
        checks.append('frozen_original_inputs_outputs_and_metadata_exclusion')
        batch_body={'commands':[command("from pathlib import Path; Path('same-name.txt').write_text('"+v+"')") for v in ('alpha','beta')], 'max_concurrent':2}
        batch=api.call('POST',api.prefix+'/jobs:batch',batch_body,expect=202,extra_headers={'Idempotency-Key':'default-batch'})
        paths=[]
        for job,value in zip(batch['jobs'],('alpha','beta')):
            done=api.wait_job(job['id']);m=done['attempt']['workspace'];paths.append(m['run_path'])
            assert (root/m['run_path']/'same-name.txt').read_text()==value
            assert (root/m['inputs_path']/'Ref/input.txt').read_text()=='new live edit'
        assert len(set(paths))==2 and not (root/'same-name.txt').exists()
        replay=api.call('POST',api.prefix+'/jobs:batch',batch_body,expect=202,extra_headers={'Idempotency-Key':'default-batch'})
        assert replay['id']==batch['id'] and [j['id'] for j in replay['jobs']]==[j['id'] for j in batch['jobs']]
        checks.append('batch_same_filename_private_outputs_and_idempotent_original_attempts')
        wsl=api.call('POST',api.prefix+'/jobs',{'runtime':'wsl','command':'cat Ref/input.txt; printf WSL_PRIVATE > wsl-output.txt'})
        done=api.wait_job(wsl['id']);m=done['attempt']['workspace']
        assert (root/m['run_path']/'wsl-output.txt').read_text()=='WSL_PRIVATE' and not (root/'wsl-output.txt').exists()
        checks.append('actual_WSL_default_attempt_directory')
        fail_body={'command':command("from pathlib import Path; print(Path('Ref/input.txt').read_text()); raise SystemExit(7)")}
        failed=api.call('POST',api.prefix+'/jobs',fail_body,extra_headers={'Idempotency-Key':'default-failure'})
        failed=api.wait_job(failed['id'],expected='failed')
        api.put('Ref/input.txt','edit after failed attempt')
        retry=api.call('POST','/fixture/retry/'+failed['attempt']['id'])
        done=api.wait_job(retry['job_id'],expected='failed');m=done['attempt']['workspace']
        assert m['source']['selection_mode']=='case' and m['source']['retry_of_attempt_id']==failed['attempt']['id']
        assert 'new live edit' in done['log_backlog'] and 'edit after failed attempt' not in done['log_backlog']
        replay=api.call('POST',api.prefix+'/jobs',fail_body,extra_headers={'Idempotency-Key':'default-failure'})
        assert replay['id']==failed['id']
        checks.append('retry_and_submission_replay_preserve_original_default_seal')
        selected=api.call('POST',api.prefix+'/jobs',{'command':command("from pathlib import Path; print(Path('Ref/input.txt').read_text())"),'input_paths':['Ref/input.txt']})
        done=api.wait_job(selected['id'])
        assert list(done['attempt']['workspace']['files'])==['Ref/input.txt']
        checks.append('explicit_input_selection_retains_its_scope')
        legacy=api.call('POST',api.prefix+'/jobs',{'type':'convert','command':command("from pathlib import Path; Path('legacy-convert.txt').write_text('legacy consumer')")})
        legacy=api.wait_job(legacy['id'])
        assert legacy['attempt']['workspace']['run_path']
        assert legacy['attempt']['outputs']['publication']['status']=='committed'
        assert (root/'legacy-convert.txt').read_text()=='legacy consumer'
        checks.append('typed_command_isolated_and_published_for_current_path_consumer')
        private=manifests[0]['run_path']+'/result.txt'
        assert api.call('GET',api.prefix+'/raw?path='+private,raw=True)==b'one'
        api.call('PUT',api.prefix+'/file',{'path':private,'content':'overwrite'},expect=409)
        api.call('POST',f'/fixture/legacy/{api.case_id}')
        before=api.call('GET',api.prefix+'/jobs')
        rejection=api.call('POST',api.prefix+'/jobs',{'command':'echo must-not-start'},expect=409)
        assert rejection['detail']['code']=='recovery_required',rejection
        assert api.call('GET',api.prefix+'/jobs')==before
        checks.append('unverified_legacy_history_rejected_before_Job_allocation_or_process_launch')
        original_prefix=api.prefix;api.create_case('other owner')
        api.call('GET',original_prefix+'/raw?path='+private,expect=(403,404))
        checks.append('private_output_owner_download_and_write_protection')
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='default-workspace-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_default_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('DEFAULT_WORKSPACE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
