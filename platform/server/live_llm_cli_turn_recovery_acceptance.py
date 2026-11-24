"""Actual DeepSeek CLI turns, API death, and durable independent turn recovery."""
import argparse,asyncio,json,shutil,time,uuid,traceback,contextlib,concurrent.futures,socket,subprocess,sys,tempfile,re
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from live_llm_acceptance import source,FIXTURE as LIVE
from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_cli_inference_acceptance import FIXTURE as INFERENCE

FIXTURE=r'''
from app.models import NativeCliTurn,NativeCliEvent,NativeCliApproval,ConversationExecution
from sqlalchemy import select
target_stage=None
original_recovery_event=codex_turns.Execution.event
original_recovery_complete=records.complete
original_recovery_prepare=sessions.Session.prepare

async def recovery_prepare(self):
 try:return await original_recovery_prepare(self)
 except BaseException as error:
  import traceback
  chain=[];current=error
  while current is not None and len(chain)<8:
   message=str(current)
   if self.secret:message=message.replace(self.secret,'[REDACTED]')
   chain.append({'type':type(current).__name__,'message':message[:600],
    'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(current.__traceback__)]})
   current=current.__cause__
  with (_base/'actual-prepare-failures.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(chain,ensure_ascii=False)+'\n')
  raise
sessions.Session.prepare=recovery_prepare

from app.services import cli_user_capacity,cli_native_usage
timed_operation(cli_user_capacity,'initialize','startup.private_initialize')
timed_operation(allocations,'allocate','startup.scope_allocate')
for name in ('reserve','acquire_node','permit_initializer','prepared'):
 timed_operation(cli_initialization_capacity,name,'initialization.'+name)
for name in ('prepare','state_prepared','register_root','register_bridge','register_native_preflight'):
 timed_operation(sessions.Session,name,'startup.'+name)
timed_operation(sessions.CodexStdioClient,'start','startup.client_start')
timed_operation(records,'activate','startup.activate')
original_recovery_start=sessions.Session.start
async def recovery_start(self):
 try:return await original_recovery_start(self)
 except BaseException as error:
  import traceback
  chain=[];current=error
  while current is not None and len(chain)<8:
   message=str(current)
   if self.secret:message=message.replace(self.secret,'[REDACTED]')
   chain.append({'type':type(current).__name__,'message':message[:600],
    'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(current.__traceback__)]})
   current=current.__cause__
  with (_base/'actual-start-failures.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(chain,ensure_ascii=False)+'\n')
  raise
sessions.Session.start=recovery_start
def timed_sync(obj,name,label):
 original=getattr(obj,name)
 def measured(*args,**kwargs):
  started=time.monotonic()
  try:return original(*args,**kwargs)
  finally:
   with (_base/'actual-startup-timing.jsonl').open('a',encoding='utf8') as stream:
    stream.write(json.dumps({'operation':label,'seconds':time.monotonic()-started})+'\n')
 setattr(obj,name,measured)
timed_sync(cli_native_usage,'scan','native.metadata_scan')
timed_sync(node_storage_budget.shutil,'disk_usage','filesystem.free_space')

async def reached(stage,identifier,session_id):
 with (_base/('turn-boundary-'+stage+'.json')).open('x',encoding='utf8') as stream:
  json.dump({'stage':stage,'turn_id':identifier,'session_id':session_id},stream);stream.flush();os.fsync(stream.fileno())
 await asyncio.Event().wait()

async def recovery_event(self,event):
 result=await original_recovery_event(self,event)
 method=event.get('method');params=event.get('params',{})
 if (target_stage=='text' and method=='item/completed' and params.get('item',{}).get('type')=='agentMessage'
  or target_stage=='approval' and method=='item/commandExecution/requestApproval'
  or target_stage=='native_terminal' and method=='turn/completed'):
  await reached(target_stage,self.id,self.session.id)
 return result
codex_turns.Execution.event=recovery_event

async def recovery_complete(owner,*args,**kwargs):
 value=await original_recovery_complete(owner,*args,**kwargs)
 if target_stage=='session_closed':
  session=sessions.SESSIONS.get(owner.id)
  if session is not None and session.turn_execution is not None:
   await reached(target_stage,session.turn_execution.id,session.id)
 return value
records.complete=recovery_complete

@app.post('/api/v1/test/turn-recovery-stage')
async def stage_inference(body:dict,user=Depends(get_current_user)):
 global target_stage
 assert body['stage'] in {'text','approval','native_terminal','session_closed'}
 target_stage=body['stage'];return {'stage':target_stage}

@app.get('/api/v1/test/turn-recovery-state/{identifier}')
async def recovery_state(identifier:str,user=Depends(get_current_user)):
 from fastapi import HTTPException
 async with SessionLocal() as db:
  row=await db.get(NativeCliTurn,identifier)
  if row is None or row.created_by!=user.id:raise HTTPException(404)
  session=await db.get(NativeCliSession,row.session_id);execution=await db.get(ConversationExecution,row.conversation_id)
  events=(await db.scalars(select(NativeCliEvent).where(NativeCliEvent.turn_id==identifier).order_by(NativeCliEvent.seq))).all()
  approvals=(await db.scalars(select(NativeCliApproval).where(NativeCliApproval.turn_id==identifier))).all()
  return {'turn':{k:getattr(row,k) for k in ('id','phase','version','session_id','conversation_id','native_status','native_turn_id','thread_id','attempts_json','request_sha256')},
   'session':records.public(session),'execution':{k:getattr(execution,k) for k in ('phase','version','backend','resource_id','owner_identity_json')},
   'events':[{'seq':v.seq,'type':v.type,'payload':json.loads(v.payload_json)} for v in events],
   'approvals':[{'id':v.id,'phase':v.phase,'request_sha256':v.request_sha256,'response_json':v.response_json} for v in approvals]}
'''
STAGES=('text','approval','native_terminal','session_closed')


def verify(configuration,*,database_url=None,stages=STAGES,runtime='wsl',store=None,browser=None):
    api=IsolatedServer('live-cli-turn-recovery',app='live_fixture:app',database_url=database_url)
    settings='import os\nos.environ.update('+repr(store.settings())+')\n' if store is not None else ''
    (api.root/'live_fixture.py').write_text(settings+LIVE+MANAGED+INFERENCE+FIXTURE,encoding='utf8')
    report={'passed':False,'mock_llm_calls':0,'simulated_llm':False,'provider':'deepseek','host':'api.deepseek.com',
        'model':configuration['model'],'runtime':runtime,'stages':[],'peers':[],'upstream_http_requests_individually_counted':False}
    def save():
        text=json.dumps(report,ensure_ascii=False,indent=2);assert configuration['key'] not in text
        (api.root/'live-result.json').write_text(text+'\n',encoding='utf8')
    def call(method,path,payload=None,expected=200,*,base=None):
        with httpx.Client(base_url=(base or api.base)+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
            response=client.request(method,path,json=payload)
            assert response.status_code==expected,(method,path,response.status_code,response.text.replace(configuration['key'],'[REDACTED]')[:800])
            return response.json()
    @contextlib.contextmanager
    def peer(stage):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        assert port!=8000;base='http://127.0.0.1:'+str(port)
        with (api.root/('peer-'+stage+'.log')).open('w',encoding='utf8') as log:
            process=subprocess.Popen([sys.executable,'-B','-m','uvicorn','live_fixture:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),
                stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with httpx.Client(trust_env=False,timeout=1) as client:
                    for _ in range(150):
                        assert process.poll() is None,'Independent API worker failed readiness'
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        time.sleep(.1)
                    else:raise TimeoutError('Independent API worker did not become ready')
                yield base
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                report['peers'].append({'stage':stage,'pid':process.pid,'api':base,'exited':process.poll() is not None});save()
    print('LIVE_CLI_TURN_RECOVERY '+str(api.root),flush=True)
    active=None
    try:
        with api:
            try:
                api.create_case('Actual CLI turn owner recovery');user=api.call('GET','/auth/me')
                if runtime=='wsl':
                    call('POST','/test/prepare-cli-inference')
                profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual turn recovery',
                    'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
                for stage in stages:
                    call('POST','/test/turn-recovery-stage',{'stage':stage})
                    nonce='ACTUAL_TURN_'+uuid.uuid4().hex;api.put('Ref/turn-recovery-proof.txt',nonce+'\n')
                    conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual '+stage,'permission_mode':'ask',
                        'provider_profile_id':profile['id'],'model':configuration['model']})
                    session=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':conv['id'],'runtime':runtime,
                        'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
                    active=session['id']
                    prompt=('请调用命令工具执行 cat Ref/turn-recovery-proof.txt，读取后只回复文件中的字符串，不修改文件。' if stage=='approval' else
                        '只回复以下字符串，不调用工具：'+nonce)
                    turn=call('POST','/codex/sessions/'+active+'/turns',{'content':prompt,'client_message_id':uuid.uuid4().hex,'timeout_seconds':180})
                    report['active_turn']={'stage':stage,'turn_id':turn['id'],'session_id':active};save()
                    marker=api.root/('turn-boundary-'+stage+'.json');deadline=time.monotonic()+120
                    while not marker.exists():
                        assert api.proc.poll() is None,'Original API exited before the selected boundary'
                        assert time.monotonic()<deadline,'Actual CLI did not reach the selected boundary'
                        time.sleep(.2)
                    boundary=json.loads(marker.read_text());assert boundary['turn_id']==turn['id']
                    route='/codex/turns/'+turn['id']+':reconcile'
                    assert call('POST',route,expected=409)['detail']['code']=='recovery_required'
                    before=call('GET','/test/turn-recovery-state/'+turn['id'])
                    if stage=='approval':assert len(before['approvals'])==1 and before['approvals'][0]['phase']=='pending'
                    else:assert nonce in ''.join(e['payload'].get('text','') for e in before['events'] if e['type']=='text.delta')
                    history=api.call('GET','/conversations/'+str(conv['id']))
                    assert not any(e['type']=='message' and e['payload'].get('role')=='assistant' for e in history['events'])
                    restores_answer=stage=='session_closed'
                    if restores_answer:
                        assert before['turn']['native_status']=='completed'
                        assert before['session']['phase']=='closed' and before['session']['cleanup_verified']
                    api.__exit__(RuntimeError,None,None);assert api.proc.poll() is not None;active=None
                    shutil.copyfile(api.root/'api.log',api.root/('original-owner-'+stage+'.log'))
                    api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
                    auth=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'other','password':'isolated-test-123'})
                    token=api.token;api.token=auth['token'];call('POST',route,expected=404);api.token=token
                    if browser is not None and restores_answer:
                        report['browser']=browser(api, {'email':user['email'],'case_title':'Actual CLI turn owner recovery',
                            'conversation_id':conv['id'],'turn_id':turn['id'],'runtime':runtime,'expected':nonce,
                            'staging':store is not None})
                        save()
                    with peer(stage) as other,concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        pending=[pool.submit(call,'POST',route,base=base) for base in (api.base,other)]
                        results=[future.result() for future in pending]
                    recovered=results[0];assert results[1]==recovered,'Independent recovery workers disagreed'
                    repeated=call('POST',route)
                    report['current_recovery']={'before':before,'first':recovered,'repeated':repeated};save()
                    assert repeated==recovered,'Repeated recovery response differs'
                    after=call('GET','/test/turn-recovery-state/'+turn['id'])
                    assert recovered['turn']['phase']==('completed' if restores_answer else 'interrupted')
                    assert recovered['execution_lock_recovered']
                    assert recovered['turn']['native_status']==before['turn']['native_status']
                    assert after['execution']['phase']=='idle' and after['execution']['resource_id'] is None
                    assert after['events'][:len(before['events'])]==before['events']
                    assert sum(e['type']=='stream.done' for e in after['events'])==1
                    assert sum(e['type']=='execution.recovered' for e in after['events'])==1
                    assert all(v['phase']=='expired' for v in after['approvals'])
                    assert after['turn']['attempts_json']==before['turn']['attempts_json']
                    history=api.call('GET','/conversations/'+str(conv['id']))
                    messages=[e['payload'] for e in history['events'] if e['type']=='message']
                    assert [e['role'] for e in messages]==(['user','assistant'] if restores_answer else ['user'])
                    if restores_answer:
                        answer=messages[1]
                        assert nonce in answer['content'] and answer['native_execution_id']==turn['id']
                        assert answer['native_turn_id']==before['turn']['native_turn_id'] and answer['native_turn_complete']
                    # Prove admission by executing another real model turn on the same conversation.
                    fresh=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':conv['id'],'runtime':runtime,
                        'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
                    active=fresh['id'];reply='AFTER_RECOVERY_'+uuid.uuid4().hex
                    followup=call('POST','/codex/sessions/'+active+'/turns',{'content':'只回复以下字符串，不调用工具：'+reply,
                        'client_message_id':uuid.uuid4().hex,'timeout_seconds':120,**({'resume':True} if restores_answer else {})})
                    deadline=time.monotonic()+180
                    while followup['phase'] not in {'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}:
                        assert time.monotonic()<deadline,'Fresh actual model turn exceeded its observation deadline'
                        time.sleep(.5);followup=api.call('GET','/codex/turns/'+followup['id'])
                    assert followup['phase']=='completed' and followup['native_status']=='completed',followup
                    history=api.call('GET','/conversations/'+str(conv['id']))
                    answers=[e['payload']['content'] for e in history['events'] if e['type']=='message' and e['payload'].get('role')=='assistant']
                    if restores_answer:
                        assert followup['thread_id']==before['turn']['thread_id']
                        assert answers==[answer['content'],reply],answers
                    else:assert answers==[reply],answers
                    assert call('POST',route)==recovered,'Old recovery changed after a new real model turn'
                    closed=call('DELETE','/codex/sessions/'+active);active=None
                    assert closed['cleanup_verified']
                    report['stages'].append({'stage':stage,'before':before,'after':after,'recovered':recovered,
                        'actual_owner_terminated':True,'other_user_refused':True,'fresh_session_closed':closed['id'],'actual_followup':followup,
                        'old_recovery_after_followup_unchanged':True,'recovered_answer':messages[1] if restores_answer else None,
                        'resumed_original_thread':restores_answer})
                    report.pop('current_recovery',None)
                    save();print('ACTUAL_TURN_RECOVERY_PASS '+stage,flush=True)
                report.pop('active_turn',None);report['passed']=True
            finally:
                if active is not None:
                    try:report['failure_cleanup']=call('DELETE','/codex/sessions/'+active)
                    except Exception as error:report['failure_cleanup_error']=str(error).replace(configuration['key'],'[REDACTED]')[:800]
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1500],
            'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:report['api_exited']=api.proc is not None and api.proc.poll() is not None;save()
    print('ACTUAL_TURN_RECOVERY_RESULT '+str(api.root/'live-result.json'),flush=True)
    return report['passed'],api.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--stage',choices=STAGES)
    parser.add_argument('--runtime',choices=('wsl','windows'),default='wsl')
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id);stages=(args.stage,) if args.stage else STAGES
    if not args.postgres:
        passed,_=await asyncio.to_thread(verify,configuration,stages=stages,runtime=args.runtime);return 0 if passed else 1
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_cliturnrecovery_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_cliturnrecovery_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='cli-turn-recovery-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False};created=False
    print('CLI_TURN_RECOVERY_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database='postgres',timeout=10)
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        passed,result=await asyncio.to_thread(verify,configuration,database_url=url,stages=stages,runtime=args.runtime)
        report.update(passed=passed,root=str(result));return 0 if passed else 1
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
