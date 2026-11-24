"""Real CLI approval, API exit, retained S3 edits, explicit recovery and inference."""
import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import httpx
from testsupport import IsolatedServer, SERVER
from s3testsupport import ObjectStore
from live_llm_acceptance import source, FIXTURE as LIVE
from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_cli_inference_acceptance import FIXTURE as INFERENCE
from live_llm_cli_approval_acceptance import FIXTURE as APPROVAL, TERMINAL

FAULT = r'''
from app.routers import terminal
from app.services import storage_admission, s3_staging_transactions, staging_cleanup
import traceback
_original_quota_busy = storage_admission.busy
def observe_quota_busy():
 path=_base/'actual-quota-busy.jsonl'
 if not path.exists() or path.stat().st_size<1024*1024:
  with path.open('a',encoding='utf8') as stream:
   stream.write(json.dumps([{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_stack()])+'\n')
 return _original_quota_busy()
storage_admission.busy=observe_quota_busy
_original_lock_owner=storage_admission.lock_owner
async def observe_owner_lock(db,user_id,**kwargs):
 result=await _original_lock_owner(db,user_id,**kwargs)
 if db.get_bind().dialect.name=='postgresql':
  from sqlalchemy import text
  pid=await db.scalar(text('SELECT pg_backend_pid()'))
  path=_base/'actual-quota-acquisitions.jsonl'
  if not path.exists() or path.stat().st_size<2*1024*1024:
   with path.open('a',encoding='utf8') as stream:
    stream.write(json.dumps({'pid':pid,'at':time.monotonic(),'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_stack()]})+'\n')
 return result
storage_admission.lock_owner=observe_owner_lock
from app.services import remote_write_budget
from app.services import owner_storage_budget
_original_owner_hold=owner_storage_budget.hold
def observe_owner_hold(*args,**kwargs):
 with (_base/'actual-owner-holds.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps({'at':time.monotonic(),'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_stack()]})+'\n')
 return _original_owner_hold(*args,**kwargs)
owner_storage_budget.hold=observe_owner_hold
_original_capacity_call=remote_write_budget.Scope.call
def observe_capacity_call(self,operation):
 path=_base/'actual-capacity-calls.jsonl'
 if not path.exists() or path.stat().st_size<2*1024*1024:
  with path.open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'operation':operation.cr_code.co_name,'at':time.monotonic(),'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_stack()]})+'\n')
 return _original_capacity_call(self,operation)
remote_write_budget.Scope.call=observe_capacity_call
_original_reserved_write=s3_staging_transactions.write_reserved
async def observe_file_write(lease,changes,*args,**kwargs):
 if kwargs.get('scope')!='files':return await _original_reserved_write(lease,changes,*args,**kwargs)
 from app.services.request_deadline import current
 deadline=current()
 record={'case_id':lease.case.id,'paths':list(changes),'started_at':time.monotonic(),
         'deadline':deadline.deadline if deadline is not None else None,'sql_commit_at':None}
 original_callback=kwargs.get('on_commit')
 def committed():
  record['sql_commit_at']=time.monotonic()
  if original_callback is not None:original_callback()
 kwargs['on_commit']=committed
 try:return await _original_reserved_write(lease,changes,*args,**kwargs)
 except BaseException as error:
  record['failure_type']=type(error).__name__
  raise
 finally:
  record['settlement_ended_at']=time.monotonic()
  record['lease_resolved']=lease.resolved
  with (_base/'actual-file-write-timing.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps(record)+'\n')
s3_staging_transactions.write_reserved=observe_file_write
@app.get('/api/v1/test/cli-quota-diagnostics')
async def quota_diagnostics(user=Depends(get_current_user)):
 from sqlalchemy import text
 async with SessionLocal() as db:
  if db.get_bind().dialect.name!='postgresql':return {'database':'sqlite'}
  active=(await db.execute(text("SELECT pid,state,xact_start,wait_event_type,wait_event FROM pg_stat_activity WHERE datname=current_database()"))).mappings().all()
  locks=(await db.execute(text("SELECT pid,locktype,classid,objid,granted FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"))).mappings().all()
  return {'activity':[dict(r) for r in active],'locks':[dict(r) for r in locks]}
@app.post('/api/v1/test/cli-storage-selection')
async def select_test_storage(body:dict, user=Depends(get_current_user)):
 settings=get_settings()
 settings.s3_bucket=body['bucket']
 return {'bucket':settings.s3_bucket}
_original_remove_directory = staging_cleanup.remove_directory
def fail_selected_cleanup(path, *args, **kwargs):
 flag = _base/'fail-staging-cleanup.json'
 if flag.exists() and Path(path).resolve() == Path(json.loads(flag.read_text())['path']).resolve():
  # Real partial removal, followed by an OS error. Production code writes its
  # own durable cleanup marker; never synthesize completion/recovery receipts.
  partial = Path(path)/'Ref/new.txt'
  partial.unlink(missing_ok=True)
  raise OSError('Injected staging removal failure')
 return _original_remove_directory(path, *args, **kwargs)
staging_cleanup.remove_directory = fail_selected_cleanup
_original_cleanup = terminal._cleanup_staging
def exit_after_selected_cleanup(path, **kwargs):
 result = _original_cleanup(path, **kwargs)
 flag = _base/'exit-after-cli-cleanup.json'
 if flag.exists() and path == json.loads(flag.read_text())['stage']:
  os._exit(81)
 return result
terminal._cleanup_staging = exit_after_selected_cleanup
'''


def verify(configuration, store, database_url=None):
    api = IsolatedServer('live-cli-staging-recovery', app='live_fixture:app', database_url=database_url)
    fixture = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n'
    (api.root/'live_fixture.py').write_text(fixture+LIVE+MANAGED+INFERENCE+APPROVAL+FAULT, encoding='utf8')
    report = {'passed': False, 'provider': 'deepseek', 'model': configuration['model'], 'runtime': 'wsl',
              'storage': 's3', 'database': 'postgresql' if database_url else 'sqlite', 'mock_llm_calls': 0, 'simulated_llm': False,
              'actual_CLI_turns_started': 0, 'upstream_HTTP_count_observed': False,
              'filesystem_fault_inputs_are_not_model_writes': True, 'checks': [], 'API_exits': []}
    active = None
    def save():
        value = json.dumps(report, ensure_ascii=False, indent=2)
        assert configuration['key'] not in value
        (api.root/'live-result.json').write_text(value+'\n', encoding='utf8')
    def call(method, path, body=None, *, expected=200, base=None):
        with httpx.Client(base_url=(base or api.base)+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                          trust_env=False, timeout=180) as client:
            until=time.monotonic()+30
            while True:
                r = client.request(method, path, json=body)
                if r.status_code!=409:break
                detail=r.json().get('detail')
                if not isinstance(detail,dict) or detail.get('code')!='storage_quota_busy' or time.monotonic()>=until:break
                # Admission explicitly refused; no model reply is substituted.
                # Retry only this documented transient response, with a limit.
                report.setdefault('quota_admission_retries',[]).append({'method':method,'path':path,'status':409})
                save();time.sleep(.25)
            assert r.status_code == expected, (method,path,r.status_code,r.text.replace(configuration['key'],'[REDACTED]')[:900])
            return r.json()
    def put(path,content):
        started=time.monotonic()
        try:return call('PUT',api.prefix+'/file',{'path':path,'content':content})
        except Exception:
            observed=api.root/'actual-file-write-timing.jsonl'
            phases=[json.loads(line) for line in observed.read_text(encoding='utf8').splitlines()] if observed.exists() else []
            report['last_failed_file_write']={'path':path,'elapsed_seconds':time.monotonic()-started,
                'observed_phases':[value for value in phases if value['started_at']>=started and path in value['paths']]}
            save()
            raise
    def restart(crash=False):
        old = api.proc
        if crash:
            try:call('POST','/test/approval-exit')
            except httpx.TransportError:pass
            else:raise AssertionError('API did not actually exit')
        api.__exit__(None,None,None)
        assert old.poll() is not None
        report['API_exits'].append(old.returncode)
        shutil.copyfile(api.root/'api.log',api.root/('api-before-restart-'+str(len(report['API_exits']))+'.log'))
        api.__enter__()
        api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
        save()
    @contextlib.contextmanager
    def peer():
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base='http://127.0.0.1:'+str(port)
        with (api.root/'peer.log').open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-m','uvicorn','live_fixture:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=api.env,
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                until=time.monotonic()+25
                with httpx.Client(timeout=1,trust_env=False) as client:
                    while True:
                        assert proc.poll() is None
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        assert time.monotonic()<until;time.sleep(.2)
                yield base
            finally:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                report['peer_exited']=proc.poll() is not None
    def begin(prompt):
        nonlocal active
        session=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':conv['id'],
            'runtime':'wsl','provider_profile_id':profile['id'],'provider_wire_api':'responses'})
        active=session['id'];report['active_session']=active;save()
        turn=call('POST','/codex/sessions/'+active+'/turns',{'content':prompt,'client_message_id':uuid.uuid4().hex,'timeout_seconds':120})
        report['actual_CLI_turns_started']+=1;report['active_turn']=turn['id'];save()
        return turn
    def pending(turn):
        until=time.monotonic()+150
        while True:
            approvals=call('GET','/codex/turns/'+turn['id']+'/approvals')
            found=[a for a in approvals if a['phase']=='pending']
            if found:
                assert len(found)==1;return found[0]
            current=call('GET','/codex/turns/'+turn['id'])
            assert current['phase'] not in TERMINAL,current
            assert time.monotonic()<until;time.sleep(.3)
    print('LIVE_CLI_STAGING_RECOVERY '+str(api.root),flush=True)
    save()
    try:
        with api:
            try:
                api.create_case('Actual CLI S3 recovery')
                email=api.call('GET','/auth/me')['email']
                call('POST','/test/prepare-cli-inference')
                profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual S3 CLI',
                    'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
                conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'S3 recovery','permission_mode':'approve_writes',
                    'provider_profile_id':profile['id'],'model':configuration['model']})
                for name,value in [('approval-proof.txt','PRIVATE_'+uuid.uuid4().hex),('change.txt','ORIGINAL'),('remove.txt','REMOVE')]:
                    put('Ref/'+name,value)
                old=begin('请调用一次命令工具执行 cat Ref/approval-proof.txt，显式设置 sandbox_permissions=require_escalated。等待用户审批，不要使用其他工具或读取方式。')
                approval=pending(old);report['actual_pending_approval']=approval;save()
                assert approval['method']=='item/commandExecution/requestApproval'
                manifests=list((api.root/'files/.staging').glob('*.manifest.json'));assert len(manifests)==1
                manifest=manifests[0];stage=manifest.with_name(manifest.name.removesuffix('.manifest.json'))
                assert (stage/'Ref/approval-proof.txt').is_file()
                nonce='RECOVERED_'+uuid.uuid4().hex
                (stage/'Ref/change.txt').write_text('LOCAL_CHANGE',encoding='utf8')
                (stage/'Ref/new.txt').write_text(nonce,encoding='utf8')
                (stage/'Ref/remove.txt').unlink()
                report['fault_input']={'created':'Ref/new.txt','modified':'Ref/change.txt','deleted':'Ref/remove.txt',
                                       'source':'actual isolated filesystem edits, not model-generated output'}
                restart(crash=True);active=None
                route='/codex/turns/'+old['id']+':reconcile'
                held=call('POST',route)
                assert not held['execution_lock_recovered'] and held['turn']['phase']=='interrupted',held
                state=call('GET','/test/approval-state/'+old['id'])
                assert state['session']['cleanup_verified'] and state['approvals'][0]['phase']=='expired'
                with peer() as base:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        repeats=[f.result() for f in [pool.submit(call,'POST',route,base=b) for b in (api.base,base)]]
                    assert all(r==held for r in repeats)
                same=call('GET','/test/approval-state/'+old['id'])
                assert same['execution']['version']==state['execution']['version']
                assert stage.exists() and manifest.exists()
                call('POST','/codex/turns/'+old['id']+'/approvals/'+approval['id']+':respond',{'decision':'accept'},expected=409)
                report['checks'].append('actual owner exit, expired approval, retained stage and stable cross-API recovery');save()
                put('Ref/change.txt','REMOTE_CHANGE')
                target='/codex/turns/'+old['id']+':reconcile-staging'
                call('POST','/test/cli-storage-selection',{'bucket':store.bucket+'-changed'})
                try:
                    changed=call('POST',target,expected=409)
                    assert changed['detail']['code']=='staging_conflict',changed
                finally:call('POST','/test/cli-storage-selection',{'bucket':store.bucket})
                report['checks'].append('changed storage bucket refused before synchronization')
                detached = stage.with_name(stage.name+'.retained-inventory-test')
                assert stage.resolve().is_relative_to((api.root/'files/.staging').resolve())
                assert not detached.exists()
                original_manifest = manifest.read_bytes()
                stage.rename(detached)
                try:
                    missing = call('POST',target,expected=409)
                    assert missing['detail']['code']=='staging_conflict', missing
                    assert manifest.read_bytes()==original_manifest
                    assert api.call('GET',api.prefix+'/file?path=Ref/change.txt')['content']=='REMOTE_CHANGE'
                    assert api.call('GET',api.prefix+'/file?path=Ref/remove.txt')['content']=='REMOVE'
                    assert (detached/'Ref/change.txt').read_text()=='LOCAL_CHANGE'
                finally:
                    detached.rename(stage)
                report['checks'].append('missing staged directory preserves remote files, retained manifest and local edits');save()
                refused=call('POST',target,expected=409)
                assert refused['detail']['code']=='staging_conflict',refused
                assert (stage/'Ref/change.txt').read_text()=='LOCAL_CHANGE'
                remote=api.call('GET',api.prefix+'/file?path=Ref/change.txt')
                assert remote['content']=='REMOTE_CHANGE'
                report['checks'].append('remote conflict preserves both versions and execution fence');save()
                # Explicitly resolve our controlled fault by restoring its baseline.
                put('Ref/change.txt','ORIGINAL')
                flag=api.root/'fail-staging-cleanup.json';flag.write_text(json.dumps({'path':str(stage)}))
                failure=call('POST',target,expected=502);report['partial_cleanup_response']=failure
                marker=stage.with_name(stage.name+'.terminal-cleanup.json')
                assert marker.exists() and json.loads(marker.read_text())['source_synchronized'] is True
                assert not (stage/'Ref/new.txt').exists()
                assert api.call('GET',api.prefix+'/file?path=Ref/new.txt')['content']==nonce
                assert api.call('GET',api.prefix+'/file?path=Ref/change.txt')['content']=='LOCAL_CHANGE'
                report['checks'].append('real remote commit precedes injected partial cleanup failure');save()
                flag.unlink();restart()
                exit_flag=api.root/'exit-after-cli-cleanup.json'
                exit_flag.write_text(json.dumps({'stage':'.staging/'+stage.name}))
                try:call('POST',target)
                except httpx.TransportError:pass
                else:raise AssertionError('Actual API did not exit after cleanup')
                assert not any(p.exists() for p in (stage,manifest,marker))
                exit_flag.unlink();restart()
                report['checks'].append('actual API exit after full cleanup before execution release');save()
                done=call('POST',target);report['recovered']=done
                assert done['staging_reconciled'] and done['execution_lock_recovered']
                assert not any(p.exists() for p in (stage,manifest,marker))
                assert call('POST',target)==done
                assert api.call('GET',api.prefix+'/file?path=Ref/new.txt')['content']==nonce
                api.call('GET',api.prefix+'/file?path=Ref/remove.txt',expect=404)
                report['checks'].append('restart resumes cleanup only, preserves uploaded file, releases fence idempotently');save()
                call('PATCH','/codex/conversations/'+str(conv['id'])+'/settings',{'provider_profile_id':profile['id'],
                    'model':configuration['model'],'permission_mode':'approve_writes'})
                fresh=begin('请实际调用命令工具执行 cat Ref/new.txt。读取后只回复其中完整字符串，不修改任何文件。')
                until=time.monotonic()+180
                while fresh['phase'] not in TERMINAL:
                    approvals=call('GET','/codex/turns/'+fresh['id']+'/approvals')
                    for request in approvals:
                        if request['phase']!='pending':continue
                        import shlex
                        words=shlex.split(request['params']['command'])
                        if len(words)==3 and words[0] in ('bash','/usr/bin/bash','/bin/bash') and words[1] in ('-c','-lc'):words=shlex.split(words[2])
                        assert words==['cat','Ref/new.txt']
                        actions=request['params']['commandActions'];assert len(actions)==1 and actions[0]['type']=='read'
                        assert actions[0]['path']==request['params']['cwd']+'/Ref/new.txt'
                        call('POST','/codex/turns/'+fresh['id']+'/approvals/'+request['id']+':respond',{'decision':'accept'})
                    assert time.monotonic()<until;time.sleep(.3);fresh=call('GET','/codex/turns/'+fresh['id'])
                report['final_turn']=fresh
                assert fresh['phase']==fresh['native_status']=='completed' and fresh['cleanup_verified'],fresh
                state=call('GET','/test/approval-state/'+fresh['id'])
                answer=''.join(e['payload'].get('text','') for e in state['events'] if e['type']=='text.delta')
                assert answer==nonce,answer
                assert any(e['type']=='item.completed' and e['payload'].get('type')=='commandExecution'
                           and e['payload'].get('exitCode')==0 and nonce in (e['payload'].get('aggregatedOutput') or '') for e in state['events'])
                history=api.call('GET','/conversations/'+str(conv['id']))
                assert any(e['type']=='message' and e['payload'].get('native_execution_id')==fresh['id']
                           and e['payload'].get('content')==nonce for e in history['events'])
                assert call('GET','/codex/conversations/'+str(conv['id'])+'/activity')['phase']=='idle'
                report['checks'].append('real CLI reads recovered remote file in new turn and persists exact answer')
                report['functional_passed']=True;save()
            finally:
                if not report.get('functional_passed') and api.proc.poll() is None:
                    try:report['failure_database_diagnostics']=call('GET','/test/cli-quota-diagnostics')
                    except Exception as diagnostic_error:report['diagnostic_error']=type(diagnostic_error).__name__
                if active and api.proc.poll() is None:
                    report['session_final']=call('DELETE','/codex/sessions/'+active)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1800]}
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None
        report['passed']=bool(report.get('functional_passed') and report['api_exited'] and report.get('peer_exited'))
        save()
    print('LIVE_CLI_STAGING_RECOVERY_RESULT '+str(api.root/'live-result.json'),flush=True)
    return report['passed'], api.root


def run(configuration, database_url=None):
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed', authenticated=True)
    result={'passed':False,'mock_llm_calls':0}
    try:
        with store:
            result['passed'],root=verify(configuration,store,database_url);result['api_root']=str(root)
    finally:
        result['store_stopped']=store.proc is not None and store.proc.poll() is not None
        result['passed']=result['passed'] and result['store_stopped']
        (store.root/'cli-recovery-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    print('CLI_STAGING_STORE_RESULT '+str(store.root/'cli-recovery-result.json'),flush=True)
    return 0 if result['passed'] else 1


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',required=True,type=int)
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    if not args.postgres:return await asyncio.to_thread(run,configuration)
    import asyncpg
    import tempfile
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in ('localhost','127.0.0.1','::1')
    name='tmp_vf_clirecovery_'+uuid.uuid4().hex
    root=Path(tempfile.mkdtemp(prefix='cli-staging-recovery-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False}
    admin=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database='postgres',timeout=10)
    created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        result=await asyncio.to_thread(run,configuration,url);report['passed']=result==0
        return result
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
            print('CLI_STAGING_POSTGRES_RESULT '+str(root/'result.json'),flush=True)

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
