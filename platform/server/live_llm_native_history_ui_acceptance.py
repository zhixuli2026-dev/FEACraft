"""Real CLI API death, history recovery UI, DeepSeek CLI turn and VTK history.

This explicit entry never executes a legacy test suite or generates a substitute
model reply. Browser actions are recorded separately from native/API evidence.
"""
import argparse, asyncio, hashlib, json, os, shutil, socket, subprocess, time, traceback, uuid
from pathlib import Path

import httpx
from testsupport import IsolatedServer, SERVER
from live_llm_acceptance import source, FIXTURE as LIVE
from cli_managed_session_checks import FIXTURE as MANAGED

ROOT=SERVER.parents[1]
EXTENSION='''
from sqlalchemy import select
from app.models import NativeSession,NativeVizSession,CliInitializationReservation
from app.services.terminal_records import public as terminal_public
from app.services.process_identity_native import observe
pending_starts=set()

@app.get('/api/v1/test/history-birth')
async def history_birth():return observe(os.getpid())
@app.post('/api/v1/test/history-exit')
async def history_exit():os._exit(0)
@app.post('/api/v1/test/history-native-root')
async def history_native_root(user=Depends(get_current_user)):
    await native_root();return {'ready':True}
@app.get('/api/v1/test/history-native-state/{identifier}')
async def history_native_state(identifier:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,identifier);assert row.created_by==user.id
        credit=await db.get(CliInitializationReservation,identifier)
        return {'id':row.id,'phase':row.phase,'version':row.version,'process':json.loads(row.process_json),
            'stop':json.loads(row.stop_json),'initialization_state':credit.state,'public':records.public(row)}
@app.post('/api/v1/test/history-initialization-boundary')
async def initialization_boundary(body:dict,user=Depends(get_current_user)):
    await native_root()
    session=sessions.Session(body['case_id'],None,user.id,runtime='wsl',provider_profile_id=body['profile_id'],provider_wire_api='responses')
    ready=asyncio.Event();release=asyncio.Event();actual=session.register_native_preflight
    async def held(value):
        result=await actual(value);ready.set();await release.wait();return result
    session.register_native_preflight=held
    task=asyncio.create_task(session.start());pending_starts.add(task);task.add_done_callback(pending_starts.discard)
    try:
        for _ in range(1200):
            if ready.is_set() or task.done():break
            await asyncio.sleep(.05)
        if task.done():await task
        assert ready.is_set(),'Actual native initializer did not reach preflight boundary'
        return {'id':session.id,'boundary':'actual_native_preflight_registered_before_root'}
    except BaseException:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);raise
@app.get('/api/v1/test/history-owned-terminals')
async def owned_terminals(user=Depends(get_current_user)):
    from app.routers.terminal import _sessions
    current=observe(os.getpid());result=[]
    async with SessionLocal() as db:
        for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id)):
            if json.loads(row.owner_identity_json)==current:
                result.append({**terminal_public(row),'owner_available':row.id in _sessions})
    return result
'''
APP=LIVE+MANAGED+EXTENSION
STL='''solid history
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 1 0 0
vertex 0 1 0
endloop
endfacet
endsolid history
'''


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(configuration,args):
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest
    assert configuration['model']=='deepseek-flash'
    api=IsolatedServer('native-history-actual-UI',app='history_fixture:app')
    (api.root/'history_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'mock_llm_calls':0,'provider':'deepseek','host':'api.deepseek.com','model':configuration['model'],
        'developer_database_modified':False,'API_exit_witnesses':[],'cli_internal_HTTP_request_count_verified':False,
        'source_review_sha256':sha(args.review)}
    handle=None;birth=None;entered=False;preview=preview_handle=preview_log=None;cli_ids=[];viz=None
    def save():
        encoded=json.dumps(report,ensure_ascii=False,indent=2);assert configuration['key'] not in encoded
        (api.root/'history-actual-result.json').write_text(encoded+'\n',encoding='utf8')
    def call(method,path,body=None):
        headers={'Authorization':'Bearer '+api.token} if api.token else {}
        with httpx.Client(base_url=api.base+'/api/v1',headers=headers,timeout=180,trust_env=False) as client:
            response=client.request(method,path,json=body)
            assert response.status_code==200,'Actual HTTP '+method+' '+path+' '+str(response.status_code)
            return response.json()
    def enter():
        nonlocal handle,birth,entered
        api.__enter__();entered=True;birth=call('GET','/test/history-birth');handle=ProcessHandle.query(birth)
    def stop():
        nonlocal handle,birth,entered
        try:
            if handle is None:
                birth=call('GET','/test/history-birth');handle=ProcessHandle.query(birth)
            try:call('POST','/test/history-exit')
            except (OSError,httpx.TransportError):pass
            handle.wait_exit(10);report['API_exit_witnesses'].append({'birth':birth,'state':'exited','proof':'retained_creation_handle'})
        finally:
            api.__exit__(RuntimeError if 'failure' in report else None,None,None);entered=False
            if handle is not None:handle.close();handle=None
            save()
    def wait_file(name,seconds=900):
        path=api.root/name;until=time.monotonic()+seconds
        while not path.exists():
            assert preview.poll() is None and api.proc.poll() is None,'Actual UI/API owner exited before browser stage completed'
            if time.monotonic()>=until:raise TimeoutError('Browser did not finish '+name)
            time.sleep(.2)
        result=json.loads(path.read_text(encoding='utf8'));assert result['passed'];return result
    print('NATIVE_HISTORY_ACTUAL_UI '+str(api.root),flush=True)
    try:
        enter();api.create_case('Actual CLI and VTK history recovery');user=call('GET','/auth/me');email=user['email']
        profile=call('POST','/provider-profiles',{'provider':'deepseek','name':'Authorized actual CLI history',
            'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
        call('POST','/test/history-native-root')
        old=call('POST','/codex/sessions',{'case_id':api.case_id,'runtime':'wsl','provider_profile_id':profile['id'],'provider_wire_api':'responses'})
        cli_ids.append(old['id']);report['old_active_cli']=call('GET','/test/history-native-state/'+old['id']);save()
        init=call('POST','/test/history-initialization-boundary',{'case_id':api.case_id,'profile_id':profile['id']})
        cli_ids.append(init['id']);report['old_initializer']=call('GET','/test/history-native-state/'+init['id']);save()
        assert old['cleanup_verified'] is not True
        stop();shutil.copyfile(api.root/'api.log',api.root/'original-owner-api.log')
        enter();api.token=call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
        old_plan=call('GET','/native-sessions/cli/'+old['id']);init_plan=call('GET','/native-sessions/cli/'+init['id'])
        assert not old_plan['owner_available'] and 'reconcile_scope' in old_plan['available_actions']
        assert not init_plan['owner_available'] and 'reconcile_initialization' in init_plan['available_actions']
        report['plans_before_UI']={'old':old_plan,'initialization':init_plan};save()
        api.put('D-post/history.stl',STL);viz=call('POST',api.prefix+'/viz:sessions',{'file_path':'D-post/history.stl'})
        report['actual_viz_id']=viz['id'];save()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        web=ROOT/'platform/web';preview_log=(api.root/'web-preview.log').open('w',encoding='utf8')
        preview=subprocess.Popen([shutil.which('node.exe') or 'node',str(web/'node_modules/vite/bin/vite.js'),'preview','--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,
            env=dict(os.environ,VITE_API_TARGET=api.base),stdout=preview_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        preview_birth=identity.observe(preview.pid);preview_handle=ProcessHandle.query(preview_birth);report['preview_birth']=preview_birth
        web_url='http://127.0.0.1:'+str(port)
        setup={'root':str(api.root),'web':web_url,'email':email,'case_id':api.case_id,'old_cli_id':old['id'],'initializer_id':init['id'],'viz_id':viz['id']}
        (api.root/'browser-setup.json').write_text(json.dumps(setup,indent=2)+'\n',encoding='utf8')
        print('NATIVE_HISTORY_RECOVERY_UI_READY '+web_url,flush=True)
        report['browser_recovery']=wait_file('browser-recovered.json')
        report['old_after_UI']=call('GET','/test/history-native-state/'+old['id']);report['initializer_after_UI']=call('GET','/test/history-native-state/'+init['id'])
        assert report['old_after_UI']['public']['cleanup_verified'] is True
        assert report['initializer_after_UI']['initialization_state']=='settled'
        report['initializer_scope_recovery']=call('POST','/codex/sessions/'+init['id']+':reconcile-scope');save()
        conv=call('POST','/conversations',{'case_id':api.case_id,'title':'Actual recovered history successor',
            'permission_mode':'plan','provider_profile_id':profile['id'],'model':configuration['model']})
        successor=call('POST','/codex/sessions',{'case_id':api.case_id,'conversation_id':conv['id'],'runtime':'wsl',
            'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
        cli_ids.append(successor['id']);nonce='REAL_HISTORY_'+uuid.uuid4().hex
        turn=call('POST','/codex/sessions/'+successor['id']+'/turns',{'content':'只回复以下字符串，不调用工具：'+nonce,
            'client_message_id':uuid.uuid4().hex,'timeout_seconds':120})
        until=time.monotonic()+180
        while turn['phase'] not in {'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}:
            if time.monotonic()>=until:raise TimeoutError('Actual CLI turn exceeded observation deadline')
            time.sleep(.5);turn=call('GET','/codex/turns/'+turn['id'])
        events=call_events(api,turn['id']);answer=''.join(row.get('payload',{}).get('text','') for row in events if row.get('type')=='text.delta')
        report['actual_cli_turn']={'turn':turn,'answer':answer,'expected':nonce,'events':events};save()
        assert turn['phase']=='completed' and turn['native_status']=='completed' and turn['cleanup_verified'] is True and answer==nonce
        history=call('GET','/conversations/'+str(conv['id']))
        assert any(row['type']=='message' and row['payload'].get('role')=='assistant' and row['payload'].get('content')==nonce for row in history['events'])
        call('DELETE','/codex/sessions/'+successor['id'])
        setup.update(successor_id=successor['id']);(api.root/'browser-successor-ready.json').write_text(json.dumps(setup,indent=2)+'\n',encoding='utf8')
        print('NATIVE_HISTORY_SUCCESSOR_UI_READY '+successor['id'],flush=True)
        report['browser_successor']=wait_file('browser-finished.json')
        links=call('GET','/native-sessions/cli/'+old['id'])['links']
        assert any(row['successor_id']==successor['id'] for row in links)
        report['actual_cli_model_turn_verified']=True;report['functional_passed']=True;save()
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]};save()
    finally:
        if entered:
            try:
                for identifier in cli_ids:
                    state=call('GET','/native-sessions/cli/'+identifier)
                    if state['cleanup_verified'] is not True:
                        if state['owner_available']:call('DELETE','/codex/sessions/'+identifier)
                        else:call('POST','/codex/sessions/'+identifier+':reconcile-scope')
                    assert call('GET','/native-sessions/cli/'+identifier)['cleanup_verified'] is True
                if viz is not None:
                    call('DELETE','/viz:sessions/'+str(viz['id']));assert call('GET','/viz:sessions/'+str(viz['id']))['cleanup_verified'] is True
                for row in call('GET','/test/history-owned-terminals'):
                    if row['cleanup_verified'] is not True:call('DELETE','/terminal/sessions/'+row['id'])
                terminals=call('GET','/test/history-owned-terminals');assert all(row['cleanup_verified'] is True for row in terminals)
                report['owned_terminal_cleanup']=terminals;report['actual_native_cleanup_verified']=True
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
            try:stop()
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
        if handle is not None:handle.close()
        if preview is not None:
            if preview.poll() is None:preview.terminate()
            preview.wait(timeout=10)
        if preview_handle is not None:preview_handle.wait_exit(10);report['preview_exited']=True;preview_handle.close()
        if preview_log is not None:preview_log.close()
        report['passed']=bool(report.get('functional_passed') and report.get('actual_native_cleanup_verified') and len(report['API_exit_witnesses'])==2 and report.get('preview_exited') and not report.get('cleanup_failures'));save()
    print('NATIVE_HISTORY_ACTUAL_RESULT '+str(api.root/'history-actual-result.json'),flush=True)
    return 0 if report['passed'] else 1


def call_events(api,identifier):
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=30,trust_env=False) as client:
        response=client.get('/codex/turns/'+identifier+'/events');response.raise_for_status()
        return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]


async def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--review',type=Path,required=True);args=parser.parse_args()
    return await asyncio.to_thread(verify,await source(args.source_profile_id),args)


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
