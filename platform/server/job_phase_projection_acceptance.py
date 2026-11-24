"""Actual native Job phase projections across two APIs and owner exit.

Three real Python processes succeed, fail and are cancelled. The only injected
faults are explicitly identified completion-metadata states after verified native
exit. No model provider, reply, CLI inference or historical test main is imported.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid

from testsupport import SERVER, IsolatedServer

ROOT=SERVER.parents[1]
APP='''import asyncio,hashlib,json,os
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select,text
from app.main import app
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import JobAttempt
from app.routers.jobs import _get_job
from app.services import jobrunner
from app.services.process_identity_native import observe
from app.services.process_stop import public as public_stop

@app.on_event('startup')
async def isolate_direct_local_delivery():
    # Stop the actual recovery dispatcher, not claim/process/state functions.
    # Official local POST still invokes the original registered Job runner.
    app.state.delivery_stop.set()
    app.state.delivery_task.cancel()
    await asyncio.gather(app.state.delivery_task,return_exceptions=True)

@app.get('/api/v1/test/phase-birth')
async def birth():return observe(os.getpid())

@app.get('/api/v1/test/phase-local/{job_id}')
async def local(job_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        await _get_job(job_id,user,db,write=False)
        proc=jobrunner.get_proc(job_id)
        return {'local_present':proc is not None,'local_phase':proc.phase if proc else None,
            'local_finished':proc.finished if proc else None,'local_error':proc.completion_error if proc else None,
            'delivery_dispatcher_stopped':app.state.delivery_task.done()}

@app.post('/api/v1/test/phase-completion/{job_id}')
async def completion_fault(job_id:int,body:dict,user=Depends(get_current_user)):
    assert set(body)=={'state'} and body['state'] in {'pending','invalid','restore'}
    async with SessionLocal() as db:
        job=await _get_job(job_id,user,db,write=True)
        assert job.case_id==1 and job.status=='succeeded'
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id).order_by(JobAttempt.number.desc()).limit(1))
        assert attempt.phase=='succeeded' and public_stop(job,attempt)['verified'] is True
        marker=Path.cwd()/('phase-completion-'+str(job_id)+'.json')
        if marker.exists():
            record=json.loads(marker.read_text(encoding='utf8'))
            assert record['attempt_id']==attempt.id and record['last']==attempt.output_manifest_json
        else:
            record={'attempt_id':attempt.id,'original':attempt.output_manifest_json,'last':attempt.output_manifest_json,'restored':True}
        if body['state']=='restore':
            updated=record['original'];record['restored']=True
        else:
            assert record['restored'] is True
            value=json.loads(record['original'])
            value['completion']={'schema':'vibeflow.job-completion.v1','state':'pending','alias_error':None,'settled_at':None} if body['state']=='pending' else 'EXPLICIT_INVALID_COMPLETION_METADATA'
            updated=json.dumps(value,sort_keys=True);record['restored']=False
        result=await db.execute(text('UPDATE job_attempts SET output_manifest_json=:new WHERE id=:id AND output_manifest_json=:old'),
            {'id':attempt.id,'old':attempt.output_manifest_json,'new':updated})
        assert result.rowcount==1
        await db.commit()
        record['last']=updated;marker.write_text(json.dumps(record),encoding='utf8')
        return {'state':body['state'],'job_id':job_id,'exact_restored':record['restored'],
            'stop_proof_and_status_unchanged':True,'metadata_only_protocol_fault':True}
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def history(path):
    db=sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True)
    try:
        return {name:hashlib.sha256(repr(db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()).encode()).hexdigest()
            for name in ['jobs','job_attempts','job_submission_requests']}
    finally:db.close()


def main():
    from app.services.windows_process_scope import ProcessHandle
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--expect',choices=['split','uniform'],required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest,name
    api=IsolatedServer('job-phase-projection',app='job_phase_fixture:app')
    (api.root/'job_phase_fixture.py').write_text(APP,encoding='utf8')
    peer=IsolatedServer('job-phase-peer',app='job_phase_fixture:app')
    report={'passed':False,'expect':args.expect,'real_model_calls':0,'mock_llm_calls':0,
        'actual_native_jobs':[],'views':{},'metadata_faults':{},'review_sha256':sha(args.review)}
    entered=peer_started=False;api_handle=peer_handle=None
    fault_job=None;stops={}

    def wait_for(check,label,seconds=120):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            result=check()
            if result:return result
            if (entered and api.proc.poll() is not None) or (peer_started and peer.proc.poll() is not None):
                raise RuntimeError('An owned API exited while waiting for '+label)
            time.sleep(.1)
        raise TimeoutError(label)

    def stopped(client,identifier,expected=None):
        def read():
            value=client.call('GET','/jobs/'+str(identifier));attempt=value.get('attempt') or {}
            if value['status'] in {'succeeded','failed','cancelled','interrupted'} and attempt.get('phase')==value['status'] and (attempt.get('process_stop') or {}).get('verified') is True:
                if expected is not None:assert value['status']==expected,(identifier,value['status'],expected)
                stops[str(identifier)]=attempt['process_stop'];return value
        return wait_for(read,'actual native stop for '+str(identifier),180)

    def snapshot(identifier):
        local=api.call('GET','/jobs/'+str(identifier));remote=peer.call('GET','/jobs/'+str(identifier))
        row_a=next(x for x in api.call('GET',api.prefix+'/jobs') if x['id']==identifier)
        row_b=next(x for x in peer.call('GET',api.prefix+'/jobs') if x['id']==identifier)
        return {'local_detail':local,'remote_detail':remote,'local_list':row_a,'remote_list':row_b,
            'local_runtime':api.call('GET','/test/phase-local/'+str(identifier)),
            'remote_runtime':peer.call('GET','/test/phase-local/'+str(identifier))}

    try:
        api.__enter__();entered=True
        report['API_birth']=api.call('GET','/test/phase-birth');api_handle=ProcessHandle.query(report['API_birth'])
        api.create_case('Actual durable Job phase contract')
        peer.token=api.token;peer.prefix=api.prefix
        peer.log=(api.root/'peer-api.log').open('w',encoding='utf8')
        peer.proc=subprocess.Popen([sys.executable,'-m','uvicorn',peer.app,'--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],
            cwd=api.root,env=api.env,stdout=peer.log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        peer_started=True
        def ready():
            try:return peer.call('GET','/api/health',api_prefix=False).get('ok')
            except OSError:return False
        wait_for(ready,'second real API startup',30)
        report['peer_birth']=peer.call('GET','/test/phase-birth');peer_handle=ProcessHandle.query(report['peer_birth'])
        commands={
            'succeeded':"print('ACTUAL_PHASE_SUCCESS',flush=True)",
            'failed':"import sys;print('ACTUAL_PHASE_FAILURE',flush=True);sys.exit(7)",
            'cancelled':"import time;print('ACTUAL_PHASE_CANCEL_READY',flush=True);time.sleep(45)",
        }
        requests={}
        for expected,code in commands.items():
            body={'type':'shell','runtime':'windows','command':subprocess.list2cmdline([sys.executable,'-u','-c',code]),'timeout_seconds':90,'max_log_bytes':1048576}
            key=str(uuid.uuid4());value=api.call('POST',api.prefix+'/jobs',body,extra_headers={'Idempotency-Key':key})
            identifier=value['id'];report['actual_native_jobs'].append(identifier);requests[identifier]=(body,key)
            if expected=='cancelled':
                wait_for(lambda:'ACTUAL_PHASE_CANCEL_READY' in api.call('GET','/jobs/'+str(identifier)).get('log_backlog',''),'real cancellation marker')
                report['running_views']=snapshot(identifier)
                peer.call('POST','/jobs/'+str(identifier)+':cancel',{})
            stopped(api,identifier,expected)
            wait_for(lambda:api.call('GET','/test/phase-local/'+str(identifier))['local_phase']=='completed','actual local completed lifecycle')
            views=snapshot(identifier)
            assert views['local_runtime']['local_present'] and not views['remote_runtime']['local_present']
            assert views['local_runtime']['delivery_dispatcher_stopped'] and views['remote_runtime']['delivery_dispatcher_stopped']
            for name in ['local_detail','remote_detail','local_list','remote_list']:
                assert views[name]['status']==expected and views[name]['attempt']['phase']==expected
            if expected=='failed':assert views['local_detail']['exit_code']==7
            report['views'][expected]=views
        before=history(api.root/'test.sqlite')
        for identifier,(body,key) in requests.items():
            replay_a=api.call('POST',api.prefix+'/jobs',body,extra_headers={'Idempotency-Key':key})
            replay_b=peer.call('POST',peer.prefix+'/jobs',body,extra_headers={'Idempotency-Key':key})
            assert replay_a['id']==replay_b['id']==identifier
            report.setdefault('idempotent_views',{})[str(identifier)]={'local':replay_a,'remote':replay_b}
        assert history(api.root/'test.sqlite')==before
        fault_job=report['actual_native_jobs'][0]
        for state in ['pending','invalid']:
            api.call('POST','/test/phase-completion/'+str(fault_job),{'state':state})
            views=snapshot(fault_job)
            for name in ['local_detail','remote_detail','local_list','remote_list']:
                assert views[name]['status']=='running' and views[name]['phase']=='finalizing' and views[name]['completion_pending'] is True
            report['metadata_faults'][state]={'views':views,'native_execution_already_stopped':True,'not_actual_finalization_fault':True}
            assert api.call('POST','/test/phase-completion/'+str(fault_job),{'state':'restore'})['exact_restored']
        assert history(api.root/'test.sqlite')==before
        report['histories_before_reads']=before
        split=all(v['local_detail']['phase']==v['local_list']['phase']=='completed' and v['remote_detail']['phase']==v['remote_list']['phase']==status for status,v in report['views'].items())
        uniform=all(all(v[n]['phase']==status for n in ['local_detail','remote_detail','local_list','remote_list']) for status,v in report['views'].items())
        assert split if args.expect=='split' else uniform
        report['counterexample_confirmed']=split;report['uniform_phase_contract']=uniform
        for identifier in report['actual_native_jobs']:stopped(peer,identifier)
        api.__exit__(None,None,None);entered=False;api_handle.wait_exit(15);api_handle.close();api_handle=None
        report['API_exit_verified']=True
        report['after_owner_exit']={str(identifier):peer.call('GET','/jobs/'+str(identifier)) for identifier in report['actual_native_jobs']}
        for status,views in report['views'].items():
            identifier=views['remote_detail']['id'];later=report['after_owner_exit'][str(identifier)]
            assert later['phase']==later['status']==status and later['attempt']['id']==views['remote_detail']['attempt']['id']
        assert history(api.root/'test.sqlite')==before
        report['all_Job_attempt_submission_rows_unchanged_by_reads_replays_fault_restore_and_owner_exit']=True
        report['functional_passed']=args.expect=='uniform'
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1400],
            'frames':[{'file':Path(row.filename).name,'line':row.lineno} for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        client=api if entered and api.proc.poll() is None else peer if peer_started and peer.proc.poll() is None else None
        if client:
            try:
                if fault_job is not None:client.call('POST','/test/phase-completion/'+str(fault_job),{'state':'restore'})
                for identifier in report['actual_native_jobs']:
                    value=client.call('GET','/jobs/'+str(identifier))
                    if value['status'] in {'queued','running'}:client.call('POST','/jobs/'+str(identifier)+':cancel',{})
                    stopped(client,identifier)
            except BaseException as error:report['cleanup_failure']=type(error).__name__+': '+str(error)[:500]
        if peer_started:peer.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if peer_handle:peer_handle.wait_exit(15);peer_handle.close();report['peer_exit_verified']=True
        if entered:api.__exit__(None if report.get('functional_passed') else RuntimeError,None,None)
        if api_handle:api_handle.wait_exit(15);api_handle.close();report['API_exit_verified']=True
        report['native_stops']=stops
        report['all_actual_native_stops_verified']=all(str(identifier) in stops for identifier in report['actual_native_jobs'])
        clean=bool(report.get('API_exit_verified') and report.get('peer_exit_verified') and report['all_actual_native_stops_verified'] and 'cleanup_failure' not in report)
        report['passed']=bool(report.get('functional_passed') and clean and 'failure' not in report)
        report['counterexample_cleanly_reproduced']=bool(args.expect=='split' and report.get('counterexample_confirmed') and clean and 'failure' not in report)
        (api.root/'phase-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('JOB_PHASE_RESULT '+str(api.root/'phase-result.json'),flush=True)
    return 0 if report['passed'] or report['counterexample_cleanly_reproduced'] else 1


if __name__=='__main__':raise SystemExit(main())
