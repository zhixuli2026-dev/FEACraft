"""Actual ConPTY close, SQLite writer failure and same-owner durable-only retry.

No model, CLI inference or invented process/stop evidence is used. The test-only
route holds a real SQLite write transaction during the real completion call.
"""
import argparse
import hashlib
import json
from pathlib import Path
import traceback

import httpx
from websockets.sync.client import connect

from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
APP = '''from app.main import app
import asyncio,json,os,sqlite3
from fastapi import Depends
from sqlalchemy import event,select
from app.db import engine,SessionLocal
from app.deps import get_current_user
from app.models import NativeSession
from app.services import terminal_records as records
from app.services.process_identity_native import observe
from app.routers.terminal import _sessions
tracked={}

@event.listens_for(engine.sync_engine,'checkout')
def short_busy_timeout(connection,record,proxy):
    cursor=connection.cursor();cursor.execute('PRAGMA busy_timeout=200');cursor.close()

@app.get('/api/v1/test/close-birth')
async def birth():return observe(os.getpid())
@app.post('/api/v1/test/close-exit')
async def exit():os._exit(0)

def memory(session):
    return {'cleanup_verified':session.cleanup_verified,'persisted_terminal':session.persisted_terminal,
        'alive':session.alive,'error':session.error,'root':session.transport.owned.record,
        'stop':session.transport.owned.stop_result,'counters':session._acceptance_counters}

@app.get('/api/v1/test/close-memory/{sid}')
async def read_memory(sid:str,user=Depends(get_current_user)):
    session=_sessions.get(sid) or tracked[sid];assert session.user_id==user.id
    return memory(session)

@app.post('/api/v1/test/close-busy/{sid}')
async def close_busy(sid:str,user=Depends(get_current_user)):
    session=_sessions[sid];assert session.user_id==user.id
    tracked[sid]=session
    counts=session._acceptance_counters={'physical_stop':0,'finalize':0,'complete':0}
    actual_stop=session.transport.stop;actual_finalize=session.finalize;actual_complete=records.complete
    async def counted_stop(*args,**kwargs):
        counts['physical_stop']+=1
        return await actual_stop(*args,**kwargs)
    async def counted_finalize(*args,**kwargs):
        counts['finalize']+=1
        return await actual_finalize(*args,**kwargs)
    async def locked_complete(*args,**kwargs):
        counts['complete']+=1
        lock=sqlite3.connect(engine.url.database,timeout=1)
        try:
            lock.execute('BEGIN IMMEDIATE')
            return await actual_complete(*args,**kwargs)
        finally:lock.rollback();lock.close()
    session.transport.stop=counted_stop;session.finalize=counted_finalize;records.complete=locked_complete
    failure=None
    try:await session.close('cancelled')
    except Exception as error:
        failure={'type':type(error).__name__,'sqlite_code':getattr(getattr(error,'orig',None),'sqlite_errorcode',None)}
    finally:records.complete=actual_complete
    return {'failure':failure,'memory':memory(session)}

@app.get('/api/v1/test/close-rows')
async def rows(user=Depends(get_current_user)):
    async with SessionLocal() as db:
        return [records.public(row) for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id))]
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    from app.services.windows_process_scope import ProcessHandle
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--expect-failure',action='store_true')
    args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest
    api=IsolatedServer('native-terminal-close-retry',app='close_retry_fixture:app')
    (api.root/'close_retry_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'expected_pre_fix_failure':args.expect_failure,'new_real_model_calls':0,
        'new_mock_llm_calls':0,'developer_database_modified':False,'source_review_sha256':sha(args.review)}
    handle=None;entered=False;sid=None
    print('NATIVE_TERMINAL_CLOSE_RETRY '+str(api.root),flush=True)
    try:
        api.__enter__();entered=True
        birth=api.call('GET','/test/close-birth');handle=ProcessHandle.query(birth)
        report['API_birth']=birth
        api.create_case('Actual terminal durable close retry')
        with connect(api.base.replace('http:','ws:')+'/ws/terminal?case='+str(api.case_id)+'&shell=cmd&token='+api.token,proxy=None) as ws:
            while True:
                frame=ws.recv(timeout=30)
                if not isinstance(frame,str):continue
                value=json.loads(frame)
                assert value.get('type')!='error','Actual terminal startup refused'
                if value.get('type')=='ready':sid=value['session'];break
        report['session_id']=sid
        first=api.call('POST','/test/close-busy/'+sid)
        assert first['failure']=={'type':'OperationalError','sqlite_code':5}
        memory=first['memory'];assert memory['cleanup_verified'] is True and memory['persisted_terminal'] is False and memory['alive'] is False
        assert memory['counters']=={'physical_stop':1,'finalize':1,'complete':1}
        before=api.call('GET','/terminal/sessions/'+sid)
        assert before['stop'] is None and before['cleanup_verified'] is None
        report.update(actual_SQLite_busy_failure=first['failure'],retained_actual_memory=memory,durable_before_retry=before)
        if args.expect_failure:
            api.call('DELETE','/terminal/sessions/'+sid,expect=502)
            assert api.call('GET','/terminal/sessions/'+sid)==before
            assert api.call('GET','/test/close-memory/'+sid)==memory
            report['known_failed_task_blocks_retry_reproduced']=True
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                values=list(pool.map(lambda _:api.call('DELETE','/terminal/sessions/'+sid),range(2)))
            assert all(value=={'ok':True} for value in values)
            after=api.call('GET','/terminal/sessions/'+sid)
            assert after['phase']=='closed' and after['cleanup_verified'] is True and after['stop']==memory['stop']
            assert after['process']['root']==memory['root'] and after['error'] is None
            final_memory=api.call('GET','/test/close-memory/'+sid)
            assert final_memory['counters']==memory['counters']
            assert final_memory['persisted_terminal'] is True and final_memory['error'] is None
            assert api.call('DELETE','/terminal/sessions/'+sid)=={'ok':True}
            assert len(api.call('GET','/test/close-rows'))==1
            report.update(durable_after_retry=after,concurrent_explicit_retry_same_root_and_stop=True,
                final_memory=final_memory,no_repeated_physical_stop_or_finalize=True,
                no_new_session=True,idempotent_closed_DELETE=True)
        report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name} for frame in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if sid and not args.expect_failure:
                    rows=api.call('GET','/test/close-rows')
                    for row in rows:
                        if row['cleanup_verified'] is not True:api.call('DELETE','/terminal/sessions/'+row['id'])
                    assert all(row['cleanup_verified'] is True for row in api.call('GET','/test/close-rows'))
                    report['all_current_owner_durable_stops_verified']=True
                try:api.call('POST','/test/close-exit')
                except (OSError,httpx.TransportError):pass
                handle.wait_exit(10);report['actual_API_exited']=True
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        if handle is not None:handle.close()
        report['passed']=bool(report.get('functional_passed') and report.get('actual_API_exited') and 'cleanup_failure' not in report)
        (api.root/'close-retry-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('NATIVE_TERMINAL_CLOSE_RETRY_RESULT '+str(api.root/'close-retry-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
