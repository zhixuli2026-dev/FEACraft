"""Actual local files/SQL, native API exit and resumed manual recovery; no LLM."""
import argparse,asyncio,hashlib,json,re,shutil,sqlite3,sys,tempfile,time,traceback,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER

ROOT=SERVER.parents[1]
APP='''from app.main import app
import os,json,uuid
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app import storage
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Job
from app.routers.workspaces import get_owned_case
from app.services import manual_file_recovery as manual
from app.services.process_identity_native import observe
from app.services.durable_events import lock_case_execution

@app.get('/api/v1/test/manual-birth')
async def birth():return observe(os.getpid())
@app.post('/api/v1/test/manual-exit')
async def exit_api():os._exit(0)
@app.post('/api/v1/test/manual-seed/{case_id}')
async def seed(case_id:int,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        await lock_case_execution(db,case_id)
        name='pending' if body['kind']=='pending' else 'preparing-'+str(uuid.uuid4())
        folder=storage._fs_path(case.s3_prefix,manual.journal.PREFIX+'/'+name)
        (folder/'before').mkdir(parents=True);(folder/'after').mkdir()
        (folder/'before/0').write_bytes(b'actual-retained-before')
        (folder/'after/0').write_bytes(b'actual-partial-after')
        (folder/'manifest.json').write_bytes(b'{"partial":')
        if body.get('unknown_job'):
            db.add(Job(case_id=case_id,type='shell',command='Explicit unverified historical metadata; no execution',status='failed'))
        if body.get('exit_after_move'):(Path.cwd()/'exit-armed').touch()
        await db.commit()
        return {'source':name}
actual_isolate=manual.isolate_local
def isolate_and_exit(case,ticket,source):
    result=actual_isolate(case,ticket,source)
    armed=Path.cwd()/'exit-armed'
    if armed.exists():
        armed.unlink();os._exit(0)
    return result
manual.isolate_local=isolate_and_exit
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run(database_url=None,pg_report=None):
    from app.services.windows_process_scope import ProcessHandle
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    if args.postgres and database_url is None:return asyncio.run(postgres())
    api=IsolatedServer('manual-file-recovery',app='manual_fixture:app',database_url=database_url);(api.root/'manual_fixture.py').write_text(APP,encoding='utf8')
    if pg_report is not None:pg_report['root']=str(api.root)
    report={'passed':False,'backend':'fs+postgresql' if database_url else 'fs+sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,'developer_database_modified':False,
        'source_review_sha256':sha(args.review),'checks':[],'API_exit_witnesses':[]}
    handles=[];entered=False
    print('MANUAL_FILE_RECOVERY '+str(api.root),flush=True)
    def save():(api.root/'manual-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def query(sql,parameters):
        if database_url:
            async def read():
                from sqlalchemy import text
                from sqlalchemy.ext.asyncio import create_async_engine
                engine=create_async_engine(database_url)
                try:
                    async with engine.connect() as connection:return [tuple(row) for row in await connection.execute(text(sql),parameters)]
                finally:await engine.dispose()
            return asyncio.run(read())
        with sqlite3.connect(api.root/'test.sqlite') as db:return db.execute(sql,parameters).fetchall()
    def body(plan,action,**extra):return {'action':action,'source':plan.get('source','pending'),'snapshot_sha256':plan['snapshot_sha256'],
        'expected_revision':plan['expected_revision'],'expected_event_sequence':plan['expected_event_sequence'],'note':'Actual manual version '+action,**extra}
    def action(value,key=None,expect=202):return api.call('POST',api.prefix+'/file-recovery/actions',value,
        extra_headers={'Idempotency-Key':key or uuid.uuid4().hex},expect=expect)
    def options(source='pending'):return api.call('GET',api.prefix+'/file-recovery?source='+source)
    def scope(name):
        workspace=api.call('GET','/workspaces')[0]
        case=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':name})
        api.case_id=case['id'];api.prefix='/cases/'+str(case['id']);return case
    def retain_handle():
        birth=api.call('GET','/test/manual-birth');handle=ProcessHandle.query(birth);handles.append((handle,birth));return handle,birth
    def witness(handle,birth):
        handle.wait_exit(10);report['API_exit_witnesses'].append({'birth':birth,'state':'exited','proof':'retained_creation_handle'})
    try:
        api.__enter__();entered=True;handle,birth=retain_handle()
        api.create_case('Manual file recovery actual');email=api.call('GET','/auth/me')['email']
        api.put('Ref/current.txt','actual-external-current');api.put('Ref/recovered.txt','existing-destination')
        api.call('POST','/test/manual-seed/'+str(api.case_id),{'kind':'pending'})
        plan=options();assert plan['commit_state']=='not_determined' and 'isolate_version' in plan['available_actions']
        keep=body(plan,'retain_for_review');key=uuid.uuid4().hex;first=action(keep,key);assert action(keep,key)==first
        assert first['result']['write_limits_relaxed'] is False
        report['checks'].append({'kind':'corrupt_partial_retained_same_receipt','ticket':first['id']})
        plan=options();isolate=body(plan,'isolate_version');key=uuid.uuid4().hex
        def send():
            with httpx.Client(timeout=30,trust_env=False) as client:
                r=client.post(api.base+'/api/v1'+api.prefix+'/file-recovery/actions',json=isolate,
                    headers={'Authorization':'Bearer '+api.token,'Idempotency-Key':key});assert r.status_code==202,r.text
                return r.json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(send);b=pool.submit(send);first,second=a.result(),b.result()
        assert first['id']==second['id'] and first['stop_evidence_created'] is False
        initial_states=[first['state'],second['state']]
        deadline=time.monotonic()+30
        while first['state']!='completed':
            assert time.monotonic()<deadline;time.sleep(.1);first=action(isolate,key)
        assert action(isolate,key)==first
        isolation_id=first['id'];retained=api.root/'files'/first['result']['retained_path']
        # Public result is case-relative, and the SQL request binds its exact root.
        prefix=query('SELECT s3_prefix FROM cases WHERE id=:case_id',{'case_id':api.case_id})[0][0]
        root=api.root/'files'/prefix;retained=root/first['result']['retained_path']
        assert not (root/'.vibeflow/file-transactions/pending').exists()
        assert (root/'Ref/current.txt').read_bytes()==b'actual-external-current'
        assert (retained/'before/0').read_bytes()==b'actual-retained-before' and (retained/'after/0').read_bytes()==b'actual-partial-after'
        assert api.call('GET',api.prefix+'/file-recovery/actions/'+isolation_id+'/file?path=before/0',raw=True)==b'actual-retained-before'
        report['checks'].append({'kind':'same_key_concurrent_one_isolation_live_untouched','ticket':isolation_id,'initial_states':initial_states})
        restore=api.call('GET',api.prefix+'/file-recovery/actions/'+isolation_id+'/restore-plan?path=before/0')
        version=api.call('GET',api.prefix+'/file:version?path=Ref/recovered.txt')
        selected=body(restore,'restore_file',isolation_id=isolation_id,file_path='before/0',target_path='Ref/recovered.txt',expected_target_sha256='0'*64)
        refused=action(selected,expect=409);assert refused['detail']['code']=='file_conflict'
        selected['expected_target_sha256']=version['content_sha256'];key=uuid.uuid4().hex;restored=action(selected,key)
        assert restored['state']=='completed' and restored['result']['new_edit'] is True and restored['old_sql_commit_rewritten'] is False
        assert api.call('GET',api.prefix+'/raw?path=Ref/recovered.txt',raw=True)==b'actual-retained-before'
        api.put('Ref/recovered.txt','newer-user-edit')
        assert action(selected,key)==restored
        assert api.call('GET',api.prefix+'/raw?path=Ref/recovered.txt',raw=True)==b'newer-user-edit'
        assert (retained/'before/0').read_bytes()==b'actual-retained-before'
        report['checks'].append({'kind':'explicit_file_choice_conditional_new_edit_replay_never_overwrites_newer','ticket':restored['id']})
        scope('Unknown writer metadata refused')
        source=api.call('POST','/test/manual-seed/'+str(api.case_id),{'kind':'preparing','unknown_job':True})['source']
        plan=options(source);assert plan['available_actions']==['retain_for_review'] and plan['blocked']['code']=='validation_job_busy'
        refused=action(body(plan,'isolate_version'),expect=409);assert refused['detail']['code']=='validation_job_busy'
        assert action(body(plan,'retain_for_review'))['result']['write_limits_relaxed'] is False
        report['checks'].append({'kind':'unverified_legacy_job_not_treated_as_stopped_no_isolation'})
        scope('Actual API exit after version move')
        api.call('POST','/test/manual-seed/'+str(api.case_id),{'kind':'pending','exit_after_move':True})
        plan=options();interrupted=body(plan,'isolate_version');key=uuid.uuid4().hex
        try:action(interrupted,key)
        except (OSError,httpx.TransportError):pass
        else:raise AssertionError('Actual API exit was not observed')
        witness(handle,birth);api.__exit__(None,None,None);entered=False
        shutil.copyfile(api.root/'api.log',api.root/'api-before-exit.log')
        api.__enter__();entered=True;handle,birth=retain_handle()
        api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
        resumed=action(interrupted,key);assert resumed['state']=='completed' and action(interrupted,key)==resumed
        assert resumed['result']['original_bytes_preserved'] is True
        count=query("SELECT COUNT(*) FROM durable_events WHERE case_id=:case_id AND type='files.manual_recovery.committed'",{'case_id':api.case_id})[0][0]
        operations=query('SELECT state FROM case_storage_operations WHERE case_id=:case_id',{'case_id':api.case_id})
        assert count==1 and all(row[0]=='settled' for row in operations)
        report['checks'].append({'kind':'actual_API_exit_after_move_same_key_new_API_finishes_once','ticket':resumed['id']})
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1800],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                try:api.call('POST','/test/manual-exit')
                except (OSError,httpx.TransportError):pass
                witness(handle,birth)
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        for item,_ in handles:item.close()
        report['passed']=bool(report.get('functional_passed') and len(report['API_exit_witnesses'])==2 and 'cleanup_failure' not in report);save()
    print('MANUAL_FILE_RECOVERY_RESULT '+str(api.root/'manual-result.json'),flush=True)
    return 0 if report['passed'] else 1


async def postgres():
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_manualfiles_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_manualfiles_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='manual-file-postgres-',dir=ROOT/'tmp'))
    kwargs={'host':selected.host,'port':selected.port or 5432,'user':selected.username,'password':selected.password,'database':'postgres','timeout':10}
    report={'passed':False,'database':name,'developer_database_modified':False};created=False
    admin=await asyncpg.connect(**kwargs)
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');report['fresh_incremental_schema_upgrade_passed']=True
        code=await asyncio.to_thread(run,url,report);report['passed']=code==0
    except BaseException as error:report['failure_type']=type(error).__name__
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
        finally:await admin.close()
        if created:
            probe=await asyncpg.connect(**kwargs)
            try:report['temporary_database_absent']=await probe.fetchval('SELECT datname FROM pg_database WHERE datname=$1',name) is None
            finally:await probe.close()
        (root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('MANUAL_FILE_POSTGRES_RESULT '+str(root/'result.json'),flush=True)
    return 0 if report['passed'] and report.get('temporary_database_absent') else 1


if __name__=='__main__':raise SystemExit(run())
