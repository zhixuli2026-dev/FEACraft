"""Actual unsealed-request refusal, explicit replacement, native file IO and audit.

Original metadata faults are explicit; all command execution and persistence are
real. No provider, model reply, tool choice or CLI inference is simulated.
"""
import argparse,hashlib,json,shutil,sqlite3,sys,time,traceback,uuid
from pathlib import Path

import httpx
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic

ROOT=SERVER.parents[1]
APP='''from app.main import app
import json,os
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Job,JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import job_execution,job_delivery
from app.services.case_write_capacity import committing
from app.services.durable_events import json_text
from app.services.process_identity_native import observe
@app.get('/api/v1/test/input-birth')
async def birth():return observe(os.getpid())
@app.post('/api/v1/test/input-exit')
async def exit():os._exit(0)
@app.post('/api/v1/test/unsealed/{case_id}')
async def unsealed(case_id:int,body:dict,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        async with committing(db,case):
            spec={'command':body['command'],'runtime':'windows','argv':None,'case_prefix':case.s3_prefix,
                'timeout_seconds':30,'max_log_bytes':1048576,'evidence_run_dir':None}
            job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
                digest=job_execution.request_hash(spec),job_type=body.get('job_type','shell'),command=body['command'],spec=spec,input_paths=['Ref/input.txt'])
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
            document=json.loads(attempt.input_manifest_json);document.pop('workspace');attempt.input_manifest_json=json_text(document)
            if body.get('unknown_worker'):attempt.worker_id='ExplicitMetadataFaultOnly'
        return {'id':job.id,'attempt_id':attempt.id}
@app.post('/api/v1/test/refuse-unsealed/{identifier}')
async def refuse(identifier:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        job=await db.get(Job,identifier);await get_owned_case(job.case_id,user,db,write=True)
    return {'result':await job_delivery.deliver(identifier)}
@app.get('/api/v1/test/input-native-requests/{case_id}')
async def native_requests(case_id:int,user=Depends(get_current_user)):
    from app.models import NativeRequest
    from app.services.native_request_records import public
    async with SessionLocal() as db:
        await get_owned_case(case_id,user,db)
        return [public(row) for row in await db.scalars(select(NativeRequest).where(NativeRequest.case_id==case_id))]
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def snapshot(path,identifier):
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        return {'job':dict(db.execute('SELECT * FROM jobs WHERE id=?',(identifier,)).fetchone()),
            'attempt':dict(db.execute('SELECT * FROM job_attempts WHERE job_id=?',(identifier,)).fetchone())}
def native_history(path):
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        return {table:hashlib.sha256(repr(db.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()).encode()).hexdigest()
            for table in ['native_sessions','native_cli_sessions','native_viz_sessions','native_session_links']}


def run():
    from app.services.windows_process_scope import ProcessHandle
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True);args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest
    api=IsolatedServer('job-input-recovery',app='input_fixture:app');(api.root/'input_fixture.py').write_text(APP,encoding='utf8')
    original=ROOT/'tmp/native-history-actual-UI-u87051io';old=original/'test.sqlite';original_sha=sha(old)
    assert json.loads((original/'history-actual-result.json').read_text(encoding='utf8'))['passed']
    with sqlite3.connect('file:'+old.as_posix()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(api.root/'test.sqlite') as target:source.backup(target)
    shutil.copytree(original/'files',api.root/'files')
    url='sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix();before_native=native_history(api.root/'test.sqlite')
    report={'passed':False,'new_real_model_calls':0,'new_mock_llm_calls':0,'developer_database_modified':False,'original_history_source_modified':False,'source_review_sha256':sha(args.review)}
    handle=None;entered=False;new_jobs=[]
    print('JOB_INPUT_RECOVERY '+str(api.root),flush=True)
    try:
        alembic(url,api.root,'stamp','c20e87f143b9')
        for operation,target in [('upgrade','head'),('downgrade','c20e87f143b9'),('upgrade','head')]:alembic(url,api.root,operation,target)
        assert native_history(api.root/'test.sqlite')==before_native
        report['actual_migration_preserved_original_native_history']=True
        api.__enter__();entered=True;birth=api.call('GET','/test/input-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        api.create_case('Explicit current input replacement')
        api.put('Ref/input.txt','original-v1')
        original_command='echo ORIGINAL_UNEXECUTED_'+uuid.uuid4().hex
        source=api.call('POST','/test/unsealed/'+str(api.case_id),{'command':original_command})
        source_id=source['id'];report['source_job_id']=source_id
        assert api.call('POST','/test/refuse-unsealed/'+str(source_id))=={'result':'input_recovery_required'}
        plan=api.call('GET','/jobs/'+str(source_id)+'/input-recovery');assert plan['available'] is True
        old_record=snapshot(api.root/'test.sqlite',source_id)
        nonce='ACTUAL_INPUT_'+uuid.uuid4().hex
        code="from pathlib import Path; print(Path('Ref/input.txt').read_text()); Path('actual-input-output.txt').write_text('"+nonce+"')"
        command='"'+sys.executable+'" -I -S -c "'+code+'"'
        def body_for(plan):return {'action':'reprepare_current_inputs','source_attempt_id':plan['source_attempt_id'],
            'source_snapshot_sha256':plan['source_snapshot_sha256'],'expected_revision':plan['expected_revision'],
            'expected_event_sequence':plan['expected_event_sequence'],'command':command,'runtime':'windows',
            'input_paths':['Ref/input.txt'],'timeout_seconds':30,'max_log_bytes':1048576,'note':'Explicit current-input recovery, original bytes not reconstructed'}
        body=body_for(plan)
        api.put('Ref/input.txt','current-v2')
        stale=api.call('POST','/jobs/'+str(source_id)+':recover-inputs',body,extra_headers={'Idempotency-Key':uuid.uuid4().hex},expect=409)
        assert stale['detail']['code']=='input_stale' and snapshot(api.root/'test.sqlite',source_id)==old_record
        plan=api.call('GET','/jobs/'+str(source_id)+'/input-recovery');body=body_for(plan);key=uuid.uuid4().hex
        bad={**body,'source_snapshot_sha256':'0'*64}
        assert api.call('POST','/jobs/'+str(source_id)+':recover-inputs',bad,extra_headers={'Idempotency-Key':uuid.uuid4().hex},expect=409)['detail']['code']=='state_conflict'
        response=api.call('POST','/jobs/'+str(source_id)+':recover-inputs',body,extra_headers={'Idempotency-Key':key},expect=202)
        target=response['id'];new_jobs.append(target);report['target_job_id']=target;receipt=response['input_recovery']
        assert target!=source_id and receipt['source_job_id']==source_id and receipt['target_job_id']==target
        assert all(receipt[k] is False for k in ['old_execution_replayed','original_inputs_reconstructed','stop_evidence_created'])
        assert receipt['new_execution_requested'] is True
        replay=api.call('POST','/jobs/'+str(source_id)+':recover-inputs',body,extra_headers={'Idempotency-Key':key},expect=202)
        assert replay['id']==target and replay['input_recovery']==receipt and replay['created'] is False
        done=api.wait_job(target,timeout=60);assert done['attempt']['process_stop']['verified'] is True
        work=done['attempt']['workspace']['run_path']
        assert api.call('GET',api.prefix+'/raw?path='+work+'/actual-input-output.txt',raw=True)==nonce.encode()
        assert api.call('GET',api.prefix+'/raw?path='+work+'/Ref/input.txt',raw=True)==b'current-v2'
        assert api.call('GET',api.prefix+'/raw?path=Ref/input.txt',raw=True)==b'current-v2'
        after=snapshot(api.root/'test.sqlite',source_id)
        assert after['job']['status']==after['attempt']['phase']=='cancelled'
        assert after['attempt']['input_manifest_json']==old_record['attempt']['input_manifest_json']
        assert after['attempt']['process_identity_json']==old_record['attempt']['process_identity_json']=='{}' and after['attempt']['claim_token'] is None
        assert api.call('GET','/jobs/'+str(source_id))['input_recoveries']==[receipt]
        assert api.call('GET','/jobs/'+str(target))['input_recoveries']==[receipt]
        blocked=api.call('POST','/jobs/'+str(source_id)+':recover-inputs',body,extra_headers={'Idempotency-Key':uuid.uuid4().hex},expect=409)
        assert blocked['detail']['code']=='replacement_exists'
        unknown=api.call('POST','/test/unsealed/'+str(api.case_id),{'command':original_command,'unknown_worker':True})
        unknown_plan=api.call('GET','/jobs/'+str(unknown['id'])+'/input-recovery');assert unknown_plan['available'] is False
        unknown_before=snapshot(api.root/'test.sqlite',unknown['id'])
        rejected=api.call('POST','/jobs/'+str(unknown['id'])+':recover-inputs',body_for(unknown_plan),extra_headers={'Idempotency-Key':uuid.uuid4().hex},expect=409)
        assert rejected['detail']['code']=='recovery_required' and snapshot(api.root/'test.sqlite',unknown['id'])==unknown_before
        workspace=api.call('GET','/workspaces')[0]
        professional_case=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':'Professional current input replacement'})
        api.case_id=professional_case['id'];api.prefix='/cases/'+str(api.case_id)
        report_nonce='ACTUAL_REGISTERED_'+uuid.uuid4().hex
        api.put('Ref/input.txt','unused-original')
        api.put('E-report/report.md','# Current input report\n\n'+report_nonce+'\n')
        professional=api.call('POST','/test/unsealed/'+str(api.case_id),{'command':'echo UNEXECUTED_OLD_PROFESSIONAL','job_type':'convert'})
        professional_before=snapshot(api.root/'test.sqlite',professional['id'])
        professional_plan=api.call('GET','/jobs/'+str(professional['id'])+'/input-recovery')
        assert professional_plan['available'] and {'key':'report.build','version':'1'} in professional_plan['handler_choices']
        prepared_body={**body_for(professional_plan),'command':None,'input_paths':None,'handler':{'key':'report.build','version':'1'},
            'parameters':{'run_dir':'C-run/case-1','mode':'markdown','allow_standalone':True},'timeout_seconds':120}
        prepared_key=uuid.uuid4().hex
        created=api.call('POST','/jobs/'+str(professional['id'])+':recover-inputs',prepared_body,
            extra_headers={'Idempotency-Key':prepared_key},expect=202)
        new_jobs.append(created['id'])
        professional_done=api.wait_job(created['id'],timeout=120)
        assert professional_done['attempt']['handler']['key']=='report.build' and professional_done['attempt']['process_stop']['verified'] is True
        professional_run=professional_done['attempt']['workspace']['run_path']
        actual_html=api.call('GET',api.prefix+'/raw?path='+professional_run+'/E-report/build/report.html',raw=True)
        assert report_nonce.encode() in actual_html
        assert snapshot(api.root/'test.sqlite',professional['id'])['attempt']['input_manifest_json']==professional_before['attempt']['input_manifest_json']
        repeated=api.call('POST','/jobs/'+str(professional['id'])+':recover-inputs',prepared_body,extra_headers={'Idempotency-Key':prepared_key},expect=202)
        assert repeated['id']==created['id'] and repeated['input_recovery']==created['input_recovery']
        native_requests=api.call('GET','/test/input-native-requests/'+str(api.case_id))
        assert native_requests and all(row['cleanup_verified'] is True for row in native_requests)
        report['actual_registered_replacement']={'source_job_id':professional['id'],'target_job_id':created['id'],
            'receipt':created['input_recovery'],'handler':'report.build','actual_HTML_contains_new_nonce':True,
            'actual_native_preparation_requests':native_requests,'same_key_replay_same_job':True}
        assert native_history(api.root/'test.sqlite')==before_native and sha(old)==original_sha
        report.update(functional_passed=True,receipt=receipt,actual_new_job=done,old_unsealed_input_manifest_preserved=True,
            original_not_claimed=True,new_current_bytes_used=True,actual_process_stop_verified=True,idempotent_same_job=True,
            stale_input_and_source_rejected=True,unknown_worker_metadata_rejected=True,one_replacement_per_original=True,
            actual_native_history_preserved=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                for identifier in new_jobs:
                    state=api.call('GET','/jobs/'+str(identifier))
                    if state['status'] in {'queued','running'}:api.call('POST','/jobs/'+str(identifier)+':cancel')
                    state=api.wait_job(identifier,timeout=60,expected='succeeded')
                    assert state['attempt']['process_stop']['verified'] is True
                report['actual_new_jobs_stop_verified']=True
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
            try:
                try:api.call('POST','/test/input-exit')
                except (OSError,httpx.TransportError):pass
                handle.wait_exit(10);report['actual_API_exited']=True
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        if handle is not None:handle.close()
        report['passed']=bool(report.get('functional_passed') and report.get('actual_API_exited') and not report.get('cleanup_failures'))
        (api.root/'input-recovery-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('JOB_INPUT_RECOVERY_RESULT '+str(api.root/'input-recovery-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
