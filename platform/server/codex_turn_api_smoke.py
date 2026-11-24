"""Durable CLI turn/approval API using real owned workers on disposable stores."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from codex_session_smoke import CONTROLLED as BASE, wait_for
from app.services import process_identity_native as identity

WORKER = r'''
import importlib.util,json,os,subprocess,sys,time
from pathlib import Path
mode=sys.argv[1];root=Path(sys.argv[2]);spec=importlib.util.spec_from_file_location('birth',sys.argv[3])
birth=importlib.util.module_from_spec(spec);spec.loader.exec_module(birth)
thread='thread-'+str(os.getpid());active=None;count=0
(root/('cli-'+str(os.getpid())+'.json')).write_text(json.dumps({'mode':mode,'birth':birth.observe(os.getpid()),'cwd':os.getcwd()}))
def send(value):print(json.dumps(value),flush=True)
def terminal(text='NATIVE_OK',status='completed',error=None):
    if status=='completed':
        item={'id':'answer-'+active['turn'],'type':'agentMessage','text':text}
        send({'method':'item/started','params':{'threadId':thread,'turnId':active['turn'],'item':{**item,'text':''}}})
        middle=max(1,len(text)//2)
        for piece in (text[:middle],text[middle:]):send({'method':'item/agentMessage/delta','params':{'threadId':thread,'turnId':active['turn'],'itemId':item['id'],'delta':piece}})
        send({'method':'item/completed','params':{'threadId':thread,'turnId':active['turn'],'item':item}})
    send({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':active['turn'],'status':status,'error':error}}})
def prompt(kind):
    params={'threadId':thread,'turnId':active['turn'],'itemId':'item-1','startedAtMs':1}
    methods={'command':'item/commandExecution/requestApproval','file':'item/fileChange/requestApproval',
             'permissions':'item/permissions/requestApproval','input':'item/tool/requestUserInput','secret':'item/tool/requestUserInput',
             'policy':'item/commandExecution/requestApproval','network':'item/commandExecution/requestApproval'}
    if kind in {'command','policy','network'}:params.update(command='echo NATIVE_APPROVED',cwd=os.getcwd())
    if kind=='policy':params['proposedExecpolicyAmendment']=['echo','NATIVE_APPROVED']
    if kind=='network':params['proposedNetworkPolicyAmendments']=[{'action':'allow','host':'fixture.local'}]
    if kind=='permissions':params.update(cwd=os.getcwd(),permissions={'network':{'enabled':True}})
    if kind in {'input','secret'}:
        params.update(isBlocking=True,questions=[{'id':'q1','header':'Choice','question':'Answer',
            'isSecret':kind=='secret','isOther':kind=='secret','options':[{'label':'A','description':'Choose A'}]}])
    send({'id':9001,'method':methods[kind],'params':params})
for line in sys.stdin:
    request=json.loads(line);method=request.get('method');rid=request.get('id');params=request.get('params',{})
    if method is None:
        with (root/'replies.log').open('a') as stream:stream.write(str(os.getpid())+'\n')
        result=request['result'];kind=active['kind']
        if kind=='secret':terminal('echo '+result['answers']['q1']['answers'][0]+' done')
        elif kind=='input':terminal('choice '+result['answers']['q1']['answers'][0])
        elif result.get('decision') in ('decline','cancel') or ('permissions' in result and not result['permissions']):terminal('DECLINED')
        else:
            target=Path.cwd()/'Ref';target.mkdir(exist_ok=True);(target/'native-effect.txt').write_text('once')
            terminal()
        continue
    if method=='initialize':result={'userAgent':'fixture/turns'}
    elif method=='initialized':continue
    elif method in {'thread/start','thread/resume'}:
        if method=='thread/resume':thread=params['threadId']
        result={'thread':{'id':thread},'model':'fixture-model'}
    elif method=='turn/start':
        count+=1;prompt_text=params['input'][0]['text'];kind=prompt_text.split('Current user message:\n')[-1];active={'turn':'native-'+str(count),'kind':kind}
        with (root/'requests.log').open('a') as stream:stream.write(json.dumps({'pid':os.getpid(),'kind':prompt_text,'client':params['clientUserMessageId']})+'\n')
        if kind=='start_wait':
            (root/'start-held').touch()
            continue
        send({'id':rid,'result':{'turn':{'id':active['turn'],'status':'inProgress'}}})
        if kind in {'command','file','permissions','input','secret','policy','network'}:prompt(kind)
        elif kind in {'hold','ignore_interrupt'}:(root/'turn-held').touch()
        elif kind=='capacity':terminal(status='failed',error={'message':'serverOverloaded'})
        elif kind=='failed':terminal(status='failed',error={'message':'fixture failure'})
        elif kind=='foreign':
            send({'method':'turn/completed','params':{'threadId':'foreign','turn':{'id':'old','status':'completed'}}})
            terminal(status='failed',error={'message':'actual failure'})
        elif kind=='foreign_approval':send({'id':9001,'method':'item/fileChange/requestApproval','params':{'threadId':'foreign','turnId':'old','itemId':'x','startedAtMs':1}})
        elif kind=='resolved':
            prompt('command')
            send({'method':'serverRequest/resolved','params':{'threadId':thread,'requestId':9001}})
            terminal()
        elif kind=='tool_output':
            item={'id':'tool-output','type':'commandExecution','command':'fixture','cwd':os.getcwd(),'status':'inProgress','aggregatedOutput':''}
            send({'method':'item/started','params':{'threadId':thread,'turnId':active['turn'],'item':item}})
            for piece in ('<analysis>RAW_', 'TOOL</analysis>\x00'):
                send({'method':'item/commandExecution/outputDelta','params':{'threadId':thread,'turnId':active['turn'],'itemId':item['id'],'delta':piece}})
            send({'method':'item/completed','params':{'threadId':thread,'turnId':active['turn'],'item':{**item,'status':'completed','aggregatedOutput':'<analysis>RAW_TOOL</analysis>\x00'}}})
            terminal()
        elif kind=='large_text':terminal('UTF8_BEGIN'+chr(0x4e2d)*50000+'UTF8_END')
        elif kind=='oversized':terminal('x'*1100000)
        elif kind.startswith('echo '):terminal(kind[5:])
        else:
            if kind=='detached':
                options={'creationflags':subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
                child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**options)
                (root/'leaf.json').write_text(json.dumps(birth.observe(child.pid)))
            terminal()
        continue
    elif method=='turn/interrupt':
        assert params=={'threadId':thread,'turnId':active['turn']}
        if active['kind']!='ignore_interrupt':terminal(status='interrupted')
        result={}
    elif method=='account/read':result={'account':None,'requiresOpenaiAuth':False}
    elif method=='model/list':result={'data':[],'nextCursor':None}
    else:result={}
    send({'id':rid,'result':result})
'''

CONTROLLED = BASE + '''
from app.services import codex_turn_records as turns,codex_turns,conversation_execution as controls
from app.routers import agent
from app.models import ProviderProfile,AgentEvent,ConversationExecution
from app.services import llm
llm._VAULT_KEY_PATH=root/'test-vault-key'
async def provider_wait(cid,user_id,profile_id,model,cancel,run_id,reasoning_effort='off'):
    (root/('api-held-'+str(cid))).touch()
    while not (root/('api-release-'+str(cid))).exists():
        if cancel.is_set():
            yield agent.sse({'type':'run.cancelled'});return
        await asyncio.sleep(.02)
    yield agent.sse({'type':'stream.done'})
agent._run_loop_inner=provider_wait
@app.post('/api/v1/fixture/profile')
async def profile(user=Depends(get_current_user)):
    async with SessionLocal() as db:
        value=ProviderProfile(user_id=user.id,name='No-network provider fixture',provider='custom',base_url='http://127.0.0.1:1',
            default_model='fixture-model',key_ciphertext=llm.encrypt_key('fixture-native-api-credential'))
        db.add(value);await db.commit();return {'id':value.id}
@app.post('/api/v1/fixture/goal/{cid}')
async def seed_goal(cid:int,user=Depends(get_current_user)):
    from app.services.tools import goal_payload
    async with SessionLocal() as db:
        await agent._append_goal_revision(db,cid,goal_payload({'objective':'Fixture goal','status':'active'}))
        await db.commit();return {'ok':True}
@app.get('/api/v1/fixture/turn-for-session/{sid}')
async def for_session(sid:str,user=Depends(get_current_user)):
    from app.models import NativeCliTurn
    async with SessionLocal() as db:return {'id':await db.scalar(select(NativeCliTurn.id).where(NativeCliTurn.session_id==sid))}
@app.post('/api/v1/fixture/revoke-turn/{identifier}')
async def revoke(identifier:str,user=Depends(get_current_user)):
    from app.models import NativeCliTurn
    async with SessionLocal() as db:
        row=await db.get(NativeCliTurn,identifier)
        await db.execute(update(ConversationExecution).where(ConversationExecution.conversation_id==row.conversation_id).values(owner_token_sha256='0'*64))
        await db.commit();return {'ok':True}
turn_finish=turns.finish
async def finishing(db,owner,*args,**kwargs):
    result=await turn_finish(db,owner,*args,**kwargs)
    if codex_turns.RUNS[owner.resource_id].session.selected['argv'][-3]=='turn_commit_failure':db.info['cli_fail_commit']=True
    return result
turns.finish=finishing
turn_start=turns.started
async def starting_turn(db,owner,*args):
    result=await turn_start(db,owner,*args)
    if codex_turns.RUNS[owner.resource_id].session.selected['argv'][-3]=='turn_start_failure':db.info['cli_fail_commit']=True
    return result
turns.started=starting_turn
public_turn=turns.public
async def reading_turn(db,row,**kwargs):
    marker=root/'hold-turn-read'
    if marker.exists() and row.phase=='running':
        marker.unlink();(root/'turn-read-held').touch()
        while not (root/'release-turn-read').exists():await asyncio.sleep(.02)
    return await public_turn(db,row,**kwargs)
turns.public=reading_turn
@app.get('/api/v1/fixture/turn/{identifier}')
async def native_details(identifier:str,user=Depends(get_current_user)):
    from app.models import NativeCliTurn,NativeCliApproval,NativeCliEvent,AgentEvent,ConversationExecution
    async with SessionLocal() as db:
        row=await db.get(NativeCliTurn,identifier)
        session=await db.get(NativeCliSession,row.session_id)
        events=(await db.scalars(select(NativeCliEvent).where(NativeCliEvent.turn_id==identifier).order_by(NativeCliEvent.seq))).all()
        approvals=(await db.scalars(select(NativeCliApproval).where(NativeCliApproval.turn_id==identifier))).all()
        history=(await db.scalars(select(AgentEvent).where(AgentEvent.conversation_id==row.conversation_id))).all()
        control=await db.get(ConversationExecution,row.conversation_id)
        return {'turn':await turns.public(db,row),'session':records.public(session),'events':[{'seq':e.seq,'type':e.type,'payload':json.loads(e.payload_json)} for e in events],
            'approvals':[{'phase':a.phase,'response':json.loads(a.response_json) if a.response_json else None} for a in approvals],
            'history':[json.loads(e.payload) for e in history], 'control':control.phase,
            'private_home':json.loads(session.profile_json)['auth_home']}
@app.post('/api/v1/fixture/expire-turn/{identifier}')
async def expire(identifier:str,user=Depends(get_current_user)):
    from app.models import NativeCliTurn,ConversationExecution
    async with SessionLocal() as db:
        row=await db.get(NativeCliTurn,identifier);control=await db.get(ConversationExecution,row.conversation_id)
        control.lease_expires_at=now()-timedelta(seconds=1);await db.commit()
        return {'ok':True}
'''


def run_database(url, folder):
    folder.mkdir(parents=True)
    if url is not None:alembic(url,folder,'upgrade','head')
    api=IsolatedServer('codex-turn-api',app='turn_controlled:app',database_url=url)
    (api.root/'cli.py').write_text(WORKER,encoding='utf8');(api.root/'turn_controlled.py').write_text(CONTROLLED,encoding='utf8')
    checks=[];receipts=[]
    with api:
        api.create_case('Native turns');workspace=api.call('GET','/workspaces')[0]['id']
        provider_id=api.call('POST','/fixture/profile')['id']
        def setup(mode='normal',permission='approve_writes',conversation=None):
            api.call('POST','/fixture/mode/'+mode)
            if conversation is None:
                case=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Native '+uuid.uuid4().hex[:8]})
                conversation=api.call('POST','/conversations',{'case_id':case['id'],'permission_mode':permission,'provider_profile_id':provider_id})
                cid=conversation['id'];case_id=case['id']
            else:cid,case_id=conversation
            session=api.call('POST','/codex/sessions',{'case_id':case_id,'conversation_id':cid,
                'provider_profile_id':provider_id,'provider_wire_api':'responses'})
            return session,(cid,case_id)
        def submit(session,content='normal',key=None,**kwargs):
            return api.call('POST','/codex/sessions/'+session['id']+'/turns',{'content':content,
                'client_message_id':key or uuid.uuid4().hex,**kwargs})
        def done(turn,expected='completed'):
            value=wait_for(lambda:api.call('GET','/codex/turns/'+turn['id']),lambda row:row['phase'] in {'completed','failed','cancelled','interrupted','recovery_required'},seconds=45)
            assert value['phase']==expected,value
            details=api.call('GET','/fixture/turn/'+turn['id']);receipts.append(details)
            if expected=='completed':
                assert details['session']['phase']=='closed' and details['session']['cleanup_verified'] and details['control']=='idle'
                assert details['events'][-1]['type']=='stream.done'
                assert [e['seq'] for e in details['events']]==list(range(1,len(details['events'])+1))
            return value,details
        session,conversation=setup()
        key=uuid.uuid4().hex;turn=submit(session,key=key);value,details=done(turn)
        assert any(item.get('role')=='assistant' and item.get('content')=='NATIVE_OK' for item in details['history'])
        same=submit(session,key=key);assert same['id']==turn['id']
        api.call('POST','/codex/sessions/'+session['id']+'/turns',{'content':'different','client_message_id':key},expect=409)
        stream=api.call('GET','/codex/turns/'+turn['id']+'/events',raw=True).decode()
        assert 'NATIVE_OK' in stream and 'stream.done' in stream
        tail=api.call('GET','/codex/turns/'+turn['id']+'/events',raw=True,extra_headers={'Last-Event-ID':str(value['last_seq'])})
        assert tail==b''
        checks.extend(['text_is_committed_before_stream_done','logical_message_id_is_idempotent_after_scope_close','sse_reconnect_only_replays_committed_cursor'])
        discovery=f'/codex/conversations/{conversation[0]}'
        listed=api.call('GET',discovery+'/turns')
        assert listed['data'][0]['id']==turn['id'] and listed['data'][0]['runtime']=='windows' and listed['next_cursor'] is None
        assert api.call('GET',discovery+'/activity')['phase']=='idle'
        found=api.call('GET',discovery+'/turns?client_message_id='+key)
        assert [item['id'] for item in found['data']]==[turn['id']]
        api.call('GET',discovery+'/turns?limit=0',expect=400)
        api.call('GET',discovery+'/turns?limit=51',expect=400)
        api.call('GET',discovery+'/turns?before='+uuid.uuid4().hex,expect=404)
        settings={'provider_profile_id':provider_id,'model':'','permission_mode':'approve_writes'}
        assert api.call('PATCH',discovery+'/settings',settings)['provider_profile_id']==provider_id
        checks.extend(['conversation_discovery_recovers_message_without_post','conversation_history_validates_bounds_and_cursor','idle_settings_preserve_owned_profile'])
        race_session,_=setup();race_turn=submit(race_session,'hold')
        wait_for(lambda:api.call('GET','/codex/turns/'+race_turn['id']),lambda row:row['phase']=='running')
        race_discovery=f"/codex/conversations/{race_session['conversation_id']}"
        live=api.call('GET',race_discovery+'/activity')
        assert live['active_turn']['id']==race_turn['id'] and live['backend']=='codex' and live['owned_by_current_user']
        api.call('PATCH',race_discovery+'/settings',settings,expect=409)
        api.call('GET',discovery+'/turns?before='+race_turn['id'],expect=404)
        checks.append('refresh_observes_owned_active_turn_and_settings_cannot_race_execution')
        (api.root/'hold-turn-read').touch()
        with ThreadPoolExecutor(1) as pool:
            reading=pool.submit(api.call,'GET','/codex/turns/'+race_turn['id'])
            wait_for(lambda:(api.root/'turn-read-held').exists(),bool)
            api.call('POST','/codex/turns/'+race_turn['id']+':cancel')
            (api.root/'release-turn-read').touch()
            assert reading.result()['phase']=='cancelled'
        (api.root/'turn-held').unlink(missing_ok=True)
        checks.append('history_read_crossing_terminal_commit_refreshes_stale_turn')
        session2,_=setup(conversation=conversation)
        resumed=submit(session2,key=uuid.uuid4().hex,resume=True);new,details=done(resumed)
        assert new['thread_id']==value['thread_id'] and new['session_id']!=value['session_id']
        checks.append('new_owned_scope_resumes_verified_native_context')
        first_page=api.call('GET',discovery+'/turns?limit=1')
        assert first_page['data'][0]['id']==resumed['id'] and first_page['next_cursor']==resumed['id']
        second_page=api.call('GET',discovery+'/turns?limit=1&before='+first_page['next_cursor'])
        assert second_page['data'][0]['id']==turn['id'] and second_page['next_cursor'] is None
        checks.append('conversation_history_paginates_without_replaying_turns')
        session3,_=setup(conversation=conversation)
        handoff=submit(session3,'fresh thread');_,handoff_details=done(handoff)
        assert any(event['type']=='context.handoff' and event['payload']['characters']>0 for event in handoff_details['events'])
        requests=[json.loads(line) for line in (api.root/'requests.log').read_text().splitlines()]
        assert 'NATIVE_OK' in requests[-1]['kind'] and requests[-1]['kind'].endswith('Current user message:\nfresh thread')
        assert handoff_details['history'][-2]['content']=='fresh thread'
        checks.append('fresh_native_thread_receives_bounded_visible_history_without_duplicate_user_text')
        session,_=setup();goal=submit(session,goal=True);value,_=done(goal)
        assert value['status']=='waiting_continue'
        checks.append('native_goal_waits_for_explicit_continue')
        for kind in ('failed','foreign','foreign_approval'):
            session,_=setup();turn=submit(session,kind);value,_=done(turn,'failed')
            if kind in {'failed','foreign'}:assert value['native_status']=='failed'
            checks.append(kind+'_does_not_fabricate_success')
        for kind in ('resolved','tool_output','large_text','oversized'):
            session,_=setup();turn=submit(session,kind);value,details=done(turn,'failed' if kind=='oversized' else 'completed')
            if kind=='resolved':
                assert [row['phase'] for row in details['approvals']]==['expired'],details
                assert any(event['type']=='approval.resolved' for event in details['events'])
            elif kind=='tool_output':
                assert ''.join(event['payload']['text'] for event in details['events'] if event['type']=='tool.output.delta')=='<analysis>RAW_TOOL</analysis>\\0'
            elif kind=='large_text':
                assert next(item['content'] for item in details['history'] if item.get('role')=='assistant')=='UTF8_BEGIN'+chr(0x4e2d)*50000+'UTF8_END'
            else:
                assert not any(item.get('role')=='assistant' for item in details['history']) and value['cleanup_verified']
            checks.append(kind+'_protocol_projection_and_scope_boundaries')
        for kind in ('command','file','permissions','input','policy','network','secret'):
            session,conversation=setup();turn=submit(session,kind)
            path='/codex/turns/'+turn['id'];forms=wait_for(lambda:api.call('GET',path+'/approvals'),lambda values:bool(values))
            approval=forms[0];respond=path+'/approvals/'+approval['id']+':respond'
            if kind in {'command','file'}:
                api.call('POST',respond,{'decision':'invented'},expect=400)
                body={'decision':'accept'}
            elif kind=='permissions':
                api.call('POST',respond,{'decision':'accept'},expect=400)
                api.call('POST',respond,{'permissions':{'fileSystem':{'write':['C:/']}}},expect=400)
                body={'permissions':{'network':{'enabled':True}},'scope':'turn','strictAutoReview':True}
            elif kind=='input':
                api.call('POST',respond,{'answers':{'q1':['not-offered']}},expect=400)
                body={'answers':{'q1':['A']}}
            elif kind=='policy':
                api.call('POST',respond,{'decision':{'acceptWithExecpolicyAmendment':{'execpolicy_amendment':['*']}}},expect=400)
                body={'decision':{'acceptWithExecpolicyAmendment':{'execpolicy_amendment':['echo','NATIVE_APPROVED']}}}
            elif kind=='network':
                body={'decision':{'applyNetworkPolicyAmendment':{'network_policy_amendment':{'action':'allow','host':'fixture.local'}}}}
            else:
                secret='PRIVATE_NATIVE_'+uuid.uuid4().hex
                assert 'options' not in approval['params']['questions'][0]
                body={'answers':{'q1':[secret]}}
            with ThreadPoolExecutor(2) as pool:
                def answer():
                    try:return api.call('POST',respond,body)
                    except AssertionError as error:
                        assert 'got 409' in str(error),str(error)
                        return None
                replies=[pool.submit(answer) for _ in range(2)];replies=[future.result() for future in replies]
                assert sum(reply is not None for reply in replies)==1,replies
            value,details=done(turn)
            api.call('POST',respond,body,expect=409)
            if kind=='secret':
                assert secret not in json.dumps(details),details
                raw=api.call('GET',path+'/events',raw=True);assert secret.encode() not in raw
                cipher=Path(details['private_home']).parent/'.redaction-secrets.bin'
                assert cipher.exists() and secret.encode() not in cipher.read_bytes()
                session2,_=setup(conversation=conversation)
                next_turn=submit(session2,'echo '+secret,resume=True);_,next_details=done(next_turn)
                assert secret not in json.dumps(next_details)
                checks.append('secret_redaction_survives_new_scope_and_explicit_resume')
            checks.append(kind+'_approval_is_bound_and_sends_once')
        session,_=setup(permission='plan');turn=submit(session,'command');path='/codex/turns/'+turn['id']
        approval=wait_for(lambda:api.call('GET',path+'/approvals'),bool)[0]
        api.call('POST',path+'/approvals/'+approval['id']+':respond',{'decision':'accept'},expect=403)
        api.call('POST',path+'/approvals/'+approval['id']+':respond',{'decision':'decline'})
        done(turn);checks.append('plan_cannot_upgrade_native_permissions')
        # Real Agent/Goal outer execution fences, with their local provider body held.
        for backend in ('api','goal'):
            session,conversation=setup();cid,case_id=conversation
            if backend=='goal':api.call('POST',f'/fixture/goal/{cid}')
            with ThreadPoolExecutor(1) as pool:
                active=pool.submit(api.call,'POST',f'/conversations/{cid}/messages' if backend=='api' else f'/conversations/{cid}/goal:run',
                    {'content':'hold provider','provider_profile_id':provider_id} if backend=='api' else {'max_turns':1},raw=True)
                wait_for(lambda:(api.root/f'api-held-{cid}').exists(),bool)
                denied=api.call('POST','/codex/sessions/'+session['id']+'/turns',{'content':'normal','client_message_id':uuid.uuid4().hex},expect=409)
                assert denied['detail']['code']=='state_conflict',denied
                (api.root/f'api-release-{cid}').touch();active.result()
            turn=submit(session,'hold');path='/codex/turns/'+turn['id']
            wait_for(lambda:api.call('GET',path),lambda row:row['phase']=='running')
            api.call('POST',f'/conversations/{cid}/messages',{'content':'cannot overlap','provider_profile_id':provider_id},expect=409)
            api.call('POST',path+':cancel')
            checks.append(backend+'_and_native_share_durable_conversation_fence')
        for kind in ('hold','ignore_interrupt','start_wait'):
            session,conversation=setup();turn=submit(session,kind);path='/codex/turns/'+turn['id']
            wait_for(lambda:(api.root/('start-held' if kind=='start_wait' else 'turn-held')).exists(),bool)
            api.call('PUT',f'/cases/{conversation[1]}/file',{'path':'Ref/during.txt','content':'case lock is free'})
            value=api.call('POST',path+':cancel');assert value['phase']=='cancelled' and value['cleanup_verified'],value
            same=submit(session,kind,key=value['client_message_id']);assert same['id']==turn['id']
            checks.append(kind+'_cancel_joins_scope_without_replay')
            (api.root/('start-held' if kind=='start_wait' else 'turn-held')).unlink(missing_ok=True)
        for foreground in (True,False):
            (api.root/'turn-held').unlink(missing_ok=True)
            session,conversation=setup()
            connection=socket.create_connection(('127.0.0.1',api.port),timeout=10)
            if foreground:
                body=json.dumps({'content':'hold','client_message_id':uuid.uuid4().hex}).encode()
                connection.sendall((f"POST /api/v1/codex/sessions/{session['id']}/turns:stream HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {api.token}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode()+body)
                wait_for(lambda:(api.root/'turn-held').exists(),bool)
                turn=api.call('GET','/fixture/turn-for-session/'+session['id'])
            else:
                turn=submit(session,'hold')
                wait_for(lambda:(api.root/'turn-held').exists(),bool)
                connection.sendall((f"GET /api/v1/codex/turns/{turn['id']}/events HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {api.token}\r\n\r\n").encode())
            connection.recv(4096);connection.shutdown(socket.SHUT_RDWR);connection.close()
            if foreground:
                done(turn,'cancelled')
            else:
                time.sleep(.25);assert api.call('GET','/codex/turns/'+turn['id'])['phase']=='running'
                api.call('POST','/codex/turns/'+turn['id']+':cancel')
            checks.append('foreground_disconnect_joins_native_turn' if foreground else 'watch_disconnect_keeps_accepted_turn_without_replay')
        session,_=setup();turn=submit(session,'command');path='/codex/turns/'+turn['id']
        approval=wait_for(lambda:api.call('GET',path+'/approvals'),bool)[0]
        api.call('POST','/fixture/revoke-turn/'+turn['id'])
        api.call('POST',path+'/approvals/'+approval['id']+':respond',{'decision':'accept'},expect=409)
        wait_for(lambda:api.call('GET','/codex/sessions/'+session['id']),lambda row:row['phase']=='failed')
        api.call('POST','/fixture/expire-turn/'+turn['id'])
        assert api.call('GET',path)['phase']=='recovery_required'
        checks.append('lost_conversation_owner_cannot_answer_or_forge_completion')
        session,_=setup();turn=submit(session,'capacity',timeout_seconds=30);value,details=done(turn)
        assert [item['status'] for item in value['attempts']]==['failed','completed']
        checks.append('capacity_retry_has_durable_distinct_attempts')
        session,_=setup();turn=submit(session,'detached');value,_=done(turn)
        leaf=json.loads((api.root/'leaf.json').read_text());assert identity.probe(leaf)['state']=='exited'
        checks.append('native_terminal_waits_for_detached_descendant_exit')
        session,_=setup('turn_start_failure');turn=submit(session);value,_=done(turn,'failed')
        assert value['native_turn_id'] is None and value['cleanup_verified']
        checks.append('native_id_registration_failure_retires_actual_scope')
        session,_=setup('turn_commit_failure');turn=submit(session)
        wait_for(lambda:api.call('GET','/codex/sessions/'+session['id']),lambda row:row['phase']=='closed')
        api.call('POST','/fixture/expire-turn/'+turn['id'])
        value=api.call('GET','/codex/turns/'+turn['id']);assert value['phase']=='recovery_required'
        stream=api.call('GET','/codex/turns/'+turn['id']+'/events',raw=True)
        assert b'stream.done' not in stream and b'execution.unavailable' in stream
        same=submit(session,key=value['client_message_id']);assert same['id']==turn['id']
        checks.append('lost_commit_remains_unknown_and_cannot_replay_effects')
        result={'passed':True,'count':len(checks),'checks':checks,'product_api':True,'fixture_cli':True,'evidence':str(api.root)}
    (api.root/'turn-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-turn-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_cliturn_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_cliturn_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await asyncio.to_thread(run_database,url.set(database=name).render_as_string(hide_password=False),root/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (root/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'counts':{key:value['count'] for key,value in results.items()},'evidence':str(root)}))


if __name__=='__main__':asyncio.run(main())
