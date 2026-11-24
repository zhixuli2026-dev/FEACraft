"""Installed CLI + authorized DeepSeek: decisions, cancellation and active replay."""
import argparse,asyncio,concurrent.futures,contextlib,hashlib,json,os,re,shlex,shutil,socket,subprocess,sys,tempfile,time,traceback,uuid
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from live_llm_acceptance import source,FIXTURE as LIVE
from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_cli_inference_acceptance import FIXTURE as INFERENCE

FIXTURE=r'''
from app.models import NativeCliTurn,NativeCliEvent,NativeCliApproval,ConversationExecution
from sqlalchemy import select
@app.get('/api/v1/test/approval-process')
async def approval_process(user=Depends(get_current_user)):return identity.observe(os.getpid())
@app.post('/api/v1/test/approval-exit')
async def approval_exit(user=Depends(get_current_user)):os._exit(79)
@app.get('/api/v1/test/approval-state/{identifier}')
async def approval_state(identifier:str,user=Depends(get_current_user)):
 from fastapi import HTTPException
 from app.services.codex_wsl_state import mounted_path
 async with SessionLocal() as db:
  row=await db.get(NativeCliTurn,identifier)
  if row is None or row.created_by!=user.id:raise HTTPException(404)
  session=await db.get(NativeCliSession,row.session_id)
  profile=json.loads(session.profile_json)
  execution=await db.get(ConversationExecution,row.conversation_id)
  events=(await db.scalars(select(NativeCliEvent).where(NativeCliEvent.turn_id==identifier).order_by(NativeCliEvent.seq))).all()
  approvals=(await db.scalars(select(NativeCliApproval).where(NativeCliApproval.turn_id==identifier))).all()
  return {'turn':{k:getattr(row,k) for k in ('id','phase','version','session_id','conversation_id','native_status','attempts_json')},
   'session':records.public(session),'cwd':mounted_path(profile['cwd'],profile['distro']) if profile['runtime']=='wsl' else profile['cwd'],
   'execution':{k:getattr(execution,k) for k in ('phase','version','backend','resource_id','owner_identity_json','owner_token_sha256')},
   'events':[{'seq':e.seq,'type':e.type,'payload':json.loads(e.payload_json)} for e in events],
   'approvals':[{'id':a.id,'phase':a.phase,'response':json.loads(a.response_json) if a.response_json else None} for a in approvals]}
original_approval_respond=sessions.CodexStdioClient._respond
async def approval_respond(self,request_id,allowed,result):
 value=await original_approval_respond(self,request_id,allowed,result)
 # Observe only a decision actually written through the original transport.
 # No synthesized request, model output or CLI inference is involved.
 with (_base/'actual-approval-deliveries.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps({'request_id':request_id,'decision':result.get('decision')})+'\n')
 return value
sessions.CodexStdioClient._respond=approval_respond
'''
TERMINAL={'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}
SOURCES=['live_llm_cli_approval_acceptance.py','live_llm_acceptance.py','cli_managed_session_checks.py','live_llm_cli_inference_acceptance.py','testsupport.py','app/services/codex_turns.py','app/services/codex_turn.py','app/services/codex_turn_records.py','app/services/codex_turn_recovery.py','app/services/codex_stdio.py','app/services/codex_approval.py','app/services/codex_sessions.py','app/services/storage_admission.py']
def hashes():return {p:hashlib.sha256((SERVER/p).read_bytes()).hexdigest() for p in SOURCES}

def verify(configuration,*,database_url=None,store=None,selected_decisions=None):
    from app.services import process_identity_native as identity
    api=IsolatedServer('live-cli-approval',app='live_fixture:app',database_url=database_url)
    settings=store.settings() if store else {}
    (api.root/'live_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+LIVE+MANAGED+INFERENCE+FIXTURE,encoding='utf8')
    report={'passed':False,'simulated_llm':False,'mock_llm_calls':0,'provider':'deepseek','host':'api.deepseek.com','model':configuration['model'],
        'database':'postgresql' if database_url else 'sqlite','storage':'s3' if store else 'fs','runtime':'wsl','developer_database_modified':False,
        'upstream_HTTP_requests_individually_counted':False,'actual_CLI_turns_started':0,'selected_decisions':selected_decisions,
        'default_scope':'Offered accept/cancel and application turn_cancel; native decline is separate explicit --decision decline, never inferred from cancellation.',
        'native_decline_verified':False,'checks':[],'API_births':[]}
    active=None;turn=None;other_token=None
    def save():
        report['source_hashes']=hashes();encoded=json.dumps(report,ensure_ascii=False,indent=2)
        assert configuration['key'] not in encoded
        (api.root/'live-result.json').write_text(encoded+'\n',encoding='utf8')
    def request(method,path,payload=None,*,base=None,token=None):
        with httpx.Client(base_url=(base or api.base)+'/api/v1',headers={'Authorization':'Bearer '+(token or api.token)},timeout=180,trust_env=False) as client:
            response=client.request(method,path,json=payload)
            try:value=response.json()
            except ValueError:value={'body':response.text[:500]}
            assert configuration['key'] not in json.dumps(value)
            return response.status_code,value
    def call(method,path,payload=None,expected=200,**kwargs):
        status,value=request(method,path,payload,**kwargs)
        assert status==expected,(method,path,status,str(value)[:800])
        return value
    def observe_api(base=None):
        birth=call('GET','/test/approval-process',base=base);report['API_births'].append(birth);save();return birth
    def exited(birth):
        until=time.monotonic()+10
        while True:
            probe=identity.probe(birth)
            if probe['state']=='exited':return probe
            assert time.monotonic()<until,probe
            time.sleep(.05)
    @contextlib.contextmanager
    def peer():
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        assert port!=8000;base='http://127.0.0.1:'+str(port);report['peer_api']=base
        with (api.root/'peer.log').open('w',encoding='utf8') as log:
            proc=subprocess.Popen([sys.executable,'-B','-m','uvicorn','live_fixture:app','--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=api.root,env=dict(api.env),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            birth=None
            try:
                until=time.monotonic()+20
                with httpx.Client(trust_env=False,timeout=1) as client:
                    while True:
                        assert proc.poll() is None,'Peer API exited during readiness'
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError,ValueError):pass
                        assert time.monotonic()<until;time.sleep(.1)
                birth=observe_api(base);yield base
            finally:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                if birth:exited(birth)
    def start(conv):
        nonlocal active,turn
        selected=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':conv['id'],'runtime':'wsl','provider_profile_id':profile['id'],'provider_wire_api':'responses'})
        active=selected['id'];report['active_session']=active;save()
        prompt='请只调用一次命令工具执行 cat Ref/approval-proof.txt，读取后只回复文件中的字符串。若审批被拒绝或取消，立即停止使用工具并说明未读取。不要用其他命令、读取方式或修改任何文件。'
        prompt+='本次是在验证真实审批交互。请在命令工具中显式设置 sandbox_permissions 为 require_escalated，并给出读取该隔离验证文件的理由。等待用户审批。'
        turn=call('POST','/codex/sessions/'+active+'/turns',{'content':prompt,'client_message_id':uuid.uuid4().hex,'timeout_seconds':120})
        report['actual_CLI_turns_started']+=1;report['active_turn']=turn['id'];save();until=time.monotonic()+120
        while True:
            approvals=call('GET','/codex/turns/'+turn['id']+'/approvals');pending=[a for a in approvals if a['phase']=='pending']
            if pending:break
            current=call('GET','/codex/turns/'+turn['id']);assert current['phase'] not in TERMINAL,('Real CLI did not request approval',current)
            assert time.monotonic()<until,'Real CLI approval observation expired';time.sleep(.2)
        assert len(approvals)==len(pending)==1
        approval=pending[0];assert approval['method']=='item/commandExecution/requestApproval'
        state=call('GET','/test/approval-state/'+turn['id']);params=approval['params']
        words=shlex.split(params['command'])
        if len(words)==3 and words[0] in {'/bin/bash','/usr/bin/bash','bash'} and words[1] in {'-c','-lc'}:words=shlex.split(words[2])
        assert words==['cat','Ref/approval-proof.txt'],params
        assert params['cwd']==state['cwd']
        actions=params.get('commandActions',[]);assert actions and all(a['type']=='read' and a['path']==state['cwd']+'/Ref/approval-proof.txt' for a in actions),params
        return turn,approval,state
    def completed(identifier):
        until=time.monotonic()+180
        while True:
            value=call('GET','/codex/turns/'+identifier)
            if value['phase'] in TERMINAL:return value
            pending=call('GET','/codex/turns/'+identifier+'/approvals')
            assert not any(a['phase']=='pending' for a in pending),'Unexpected extra model command approval'
            assert time.monotonic()<until,'Real CLI terminal observation expired';time.sleep(.3)
    def deliveries():
        p=api.root/'actual-approval-deliveries.jsonl';return [json.loads(v) for v in p.read_text(encoding='utf8').splitlines()] if p.exists() else []
    def scenario(conv,decision,nonce,fresh,approval,reply):
        nonlocal active,turn
        before_deliveries=len(deliveries())
        if decision=='turn_cancel':
            responses=[call('POST','/codex/turns/'+fresh['id']+':cancel')]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                responses=[f.result() for f in [pool.submit(request,'POST',reply,{'decision':decision}) for _ in range(2)]]
            assert sorted(r[0] for r in responses)==[200,409],responses
        terminal=completed(fresh['id']);state=call('GET','/test/approval-state/'+fresh['id'])
        assert terminal['cleanup_verified'] and state['session']['cleanup_verified']
        if decision in {'accept','decline'}:assert terminal['phase']==terminal['native_status']=='completed',terminal
        elif decision=='turn_cancel':assert terminal['phase']=='cancelled' and terminal['native_status']=='interrupted',terminal
        else:assert terminal['phase'] in {'completed','interrupted'} and terminal['native_status'] in {'completed','interrupted'},terminal
        events=state['events'];commands=[e['payload'] for e in events if e['type']=='item.completed' and e['payload'].get('type')=='commandExecution']
        answers=''.join(e['payload'].get('text','') for e in events if e['type']=='text.delta')
        if decision=='accept':assert any(c.get('exitCode')==0 and nonce in (c.get('aggregatedOutput') or '') for c in commands) and nonce in answers,commands
        else:
            assert nonce not in answers and all(nonce not in (c.get('aggregatedOutput') or '') for c in commands)
            assert not any(c.get('exitCode')==0 for c in commands),commands
            if decision=='decline':assert any(c.get('status')=='declined' for c in commands),commands
        new_deliveries=deliveries()[before_deliveries:]
        if decision=='turn_cancel':assert not new_deliveries and state['approvals'][0]['phase']=='expired'
        else:assert len(new_deliveries)==1 and new_deliveries[0]['decision']==decision and state['approvals'][0]['phase']=='sent',new_deliveries
        assert sum(e['type']=='approval.claimed' for e in events)==(0 if decision=='turn_cancel' else 1)
        assert sum(e['type']=='stream.done' for e in events)==1
        call('POST',reply,{'decision':'accept'},expected=409)
        closed=call('DELETE','/codex/sessions/'+active);assert closed['cleanup_verified']
        active=None;turn=None;report.pop('active_session',None);report.pop('active_turn',None)
        report['checks'].append({'check':'actual_native_'+decision,'approval':approval,'responses':responses,'terminal':terminal,'state':state,'actual_native_reply_deliveries':new_deliveries,'late_reply_refused':True})
        if decision=='decline':report['native_decline_verified']=True
        save();print('LIVE_CLI_APPROVAL_PASS '+decision,flush=True)
    print('LIVE_CLI_APPROVAL '+str(api.root),flush=True)
    save()
    try:
        with api:
            try:
                api.create_case('Actual CLI approval and active old recovery');user=api.call('GET','/auth/me');observe_api()
                other_email=uuid.uuid4().hex+'@example.com'
                other=api.call('POST','/auth/register',{'email':other_email,'name':'other approval user','password':'isolated-test-123'});other_token=other['token']
                call('POST','/test/prepare-cli-inference')
                profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual CLI approval','base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
                def conversation(mode):return api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual '+mode,'permission_mode':mode,'provider_profile_id':profile['id'],'model':configuration['model']})
                if selected_decisions is None:
                    conv=conversation('ask');api.put('Ref/approval-proof.txt','ORIGINAL_'+uuid.uuid4().hex+'\n')
                    old,old_approval,before=start(conv);route='/codex/turns/'+old['id']+':reconcile'
                    call('POST',route,expected=409);old_birth=report['API_births'][-1]
                    try:request('POST','/test/approval-exit')
                    except httpx.TransportError:pass
                    else:raise AssertionError('Actual API exit unexpectedly returned a response')
                    report['old_owner_exit']=exited(old_birth);active=None;turn=None
                    api.__exit__(RuntimeError,None,None);shutil.copyfile(api.root/'api.log',api.root/'original-owner.log')
                    api.__enter__();api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token'];observe_api()
                    other_token=api.call('POST','/auth/login',{'email':other_email,'password':'isolated-test-123'})['token']
                    with peer() as base:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                            recovered=[f.result() for f in [pool.submit(call,'POST',route,base=at) for at in (api.base,base)]]
                        assert recovered[0]==recovered[1] and recovered[0]['execution_lock_recovered']
                        report['old_concurrent_recovery']=recovered[0];save()
                        old_after=call('GET','/test/approval-state/'+old['id']);assert old_after['turn']['phase']=='interrupted' and old_after['approvals'][0]['phase']=='expired'
                        late='/codex/turns/'+old['id']+'/approvals/'+old_approval['id']+':respond';call('POST',late,{'decision':'accept'},expected=409)
                        nonce='NOT_IN_PROMPT_'+uuid.uuid4().hex;api.put('Ref/approval-proof.txt',nonce+'\n')
                        fresh,approval,state=start(conv);reply='/codex/turns/'+fresh['id']+'/approvals/'+approval['id']+':respond'
                        call('POST',reply,{'decision':'accept'},expected=403)
                        call('POST',reply,{'decision':'not_a_decision'},expected=400)
                        call('POST',reply,{'decision':'cancel'},expected=404,token=other_token)
                        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                            replay=[f.result() for f in [pool.submit(call,'POST',route,base=at) for at in (api.base,base)]]
                        assert replay==recovered
                        during=call('GET','/test/approval-state/'+fresh['id'])
                        for key in ('phase','backend','resource_id','owner_identity_json','owner_token_sha256'):assert during['execution'][key]==state['execution'][key],key
                        assert during['execution']['phase']=='active' and during['execution']['resource_id']==fresh['id'] and during['approvals'][0]['phase']=='pending'
                        assert call('GET','/test/approval-state/'+old['id'])['events']==old_after['events']
                        report['checks'].append({'check':'old_recovery_replay_during_actual_new_pending_approval_preserves_new_fence','before':state,'during':during,'old_recovery':recovered[0],'grant_403_invalid_400_other_user_404':True})
                        save()
                        scenario(conv,'cancel',nonce,fresh,approval,reply)
                for decision in selected_decisions or ('accept','turn_cancel'):
                    escalated=decision in {'accept','decline'}
                    conv=conversation('approve_writes' if escalated else 'ask');nonce='NOT_IN_PROMPT_'+uuid.uuid4().hex;api.put('Ref/approval-proof.txt',nonce+'\n')
                    fresh,approval,_=start(conv);reply='/codex/turns/'+fresh['id']+'/approvals/'+approval['id']+':respond'
                    scenario(conv,decision,nonce,fresh,approval,reply)
                report['functional_passed']=True
            finally:
                if active is not None:
                    try:report['failure_cleanup']=call('DELETE','/codex/sessions/'+active)
                    except Exception as error:report['failure_cleanup_error']=str(error).replace(configuration['key'],'[REDACTED]')[:800]
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1400],'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        report['API_exit_probes']=[exited(b) for b in report['API_births']]
        report['passed']=bool(report.get('functional_passed') and all(v['state']=='exited' for v in report['API_exit_probes']))
        report['actual_approval_deliveries']=deliveries();save()
    print('LIVE_CLI_APPROVAL_RESULT '+str(api.root/'live-result.json'),flush=True)
    return report['passed'],api.root

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');parser.add_argument('--decision',action='append',choices=['accept','decline','cancel','turn_cancel']);args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    async def run(url=None):
        def invoke():
            if not args.s3:return verify(configuration,database_url=url,selected_decisions=args.decision)
            from s3testsupport import ObjectStore
            store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed');report={'passed':False}
            print('CLI_APPROVAL_S3 '+str(store.root),flush=True)
            try:
                with store:
                    passed,root=verify(configuration,database_url=url,store=store,selected_decisions=args.decision);report.update(passed=passed,api_root=str(root));return passed,root
            finally:report['store_stopped']=store.proc is not None and store.proc.poll() is not None;(store.root/'cli-approval-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        return await asyncio.to_thread(invoke)
    if not args.postgres:return 0 if (await run())[0] else 1
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url);assert selected.drivername=='postgresql+asyncpg' and selected.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='cli-approval-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False};created=False
    print('CLI_APPROVAL_POSTGRES '+str(root),flush=True)
    conn=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    try:
        await conn.execute('CREATE DATABASE "'+name+'"');created=True;url=selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');passed,api=await run(url);report.update(passed=passed,api_root=str(api));return 0 if passed else 1
    finally:
        try:
            if created:await conn.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:await conn.close();(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
