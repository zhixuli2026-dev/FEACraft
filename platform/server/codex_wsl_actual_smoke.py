"""Real WSL CLI product turns against an owned Linux loopback Responses fixture."""
import asyncio
import inspect
import json
from pathlib import Path
import time
import uuid

from testsupport import IsolatedServer
from codex_session_smoke import wait_for
from codex_turn_actual_smoke import Provider
from codex_wsl_test_support import NativeStateFixture
from app.services.codex_stdio import CodexStdioClient
from app.services.codex_environment import create_isolated_codex_environment
from app.services.owned_process import wsl_path

DRIVER = r'''
Provider.shell_command = 'if [ -n "${VIBEFLOW_CODEX_PROVIDER_TOKEN+x}" ]; then echo CREDENTIAL_LEAK; else echo CREDENTIAL_ISOLATED; fi'
provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
threading.Thread(target=provider.serve_forever,daemon=True).start()
for line in sys.stdin:
    value=json.loads(line);rid=value.get('id');method=value.get('method');params=value.get('params',{})
    if method=='initialized':continue
    if method=='initialize':result={'userAgent':'linux-loopback-responses-fixture','port':provider.server_port,'secret':Provider.secret}
    elif method=='mode':Provider.behavior=params['value'];result={}
    elif method=='release':Provider.release.set();result={}
    elif method=='status':result={'calls':Provider.calls,'entered':Provider.entered.is_set(),'environment_verified':Provider.environment_verified}
    else:raise ValueError('Unknown fixture request')
    print(json.dumps({'id':rid,'result':result}),flush=True)
'''


def exercise(api, provider, loop, native_state):
    receipts, checks = [], []
    def control(method, params=None):
        return asyncio.run_coroutine_threadsafe(provider.request(method, params), loop).result(15)
    secret = provider.initialize_result['secret']
    port = provider.initialize_result['port']
    try:
        with api:
            case = api.create_case('WSL native API turns')
            profile = api.call('POST','/provider-profiles',{'provider':'custom','name':'Linux loopback fixture',
                'base_url':f'http://127.0.0.1:{port}/v1','default_model':'fixture-model','api_key':secret})
            conv = api.call('POST','/conversations',{'case_id':case['id'],'permission_mode':'plan'})
            def session():
                return api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],
                    'provider_profile_id':profile['id'],'provider_wire_api':'responses','runtime':'wsl'})
            def submit(current, **kwargs):
                return api.call('POST','/codex/sessions/'+current['id']+'/turns',{
                    'content':'Run the local fixture.','client_message_id':uuid.uuid4().hex,**kwargs})
            def done(turn, expected='completed'):
                row=wait_for(lambda:api.call('GET','/codex/turns/'+turn['id']),
                    lambda value:value['phase'] in {'completed','failed','cancelled','interrupted'},seconds=50)
                assert row['phase']==expected and row['cleanup_verified'],row
                parent=api.call('GET','/codex/sessions/'+row['session_id'])
                assert parent['runtime']=='wsl' and parent['stop']['native_state']=='exited'
                assert parent['process']['root']['native']['process']['uid']!=0
                stream=api.call('GET','/codex/turns/'+row['id']+'/events',raw=True).decode('utf8')
                assert 'stream.done' in stream
                receipts.append({'turn':row,'session':parent,'events':stream})
                return row,stream
            current=session()
            assert api.call('GET','/codex/sessions/'+current['id']+'/account')['credential']['provider_profile_id']==profile['id']
            first,stream=done(submit(current))
            assert 'ACTUAL_TURN_OK' in stream
            checks.append('real_linux_cli_text_stream_nonroot_and_verified_exit')
            resumed,_=done(submit(session(),resume=True))
            assert resumed['thread_id']==first['thread_id'] and resumed['session_id']!=first['session_id']
            checks.append('native_home_thread_resume_with_linux_cwd')
            control('mode',{'value':'question'})
            question=submit(session(),resume=True)
            path='/codex/turns/'+question['id']
            pending=wait_for(lambda:api.call('GET',path+'/approvals'),bool,seconds=30)[0]
            assert pending['method']=='item/tool/requestUserInput'
            api.call('POST',path+'/approvals/'+pending['id']+':respond',{'answers':{'fixture_choice':['A']}})
            _,stream=done(question)
            assert 'ACTUAL_QUESTION_OK' in stream
            checks.append('native_linux_question_approval_roundtrip')
            control('mode',{'value':'environment'})
            conv=api.call('POST','/conversations',{'case_id':case['id'],'permission_mode':'full_access'})
            done(submit(session()))
            assert control('status')['environment_verified']
            checks.append('real_linux_shell_does_not_inherit_provider_credential')
            control('mode',{'value':'failure'})
            done(submit(session()),'failed')
            checks.append('native_provider_failure_is_not_completed')
            control('mode',{'value':'block'})
            blocked=submit(session())
            wait_for(lambda:control('status')['entered'],bool,seconds=25)
            at=time.monotonic()
            api.call('POST','/codex/turns/'+blocked['id']+':cancel')
            done(blocked,'cancelled')
            assert time.monotonic()-at<15
            checks.append('native_cancel_acknowledgment_then_scope_exit')
            status=control('status')
            assert status['calls'] and all(x['bound_authorization_verified'] for x in status['calls'])
            assert secret not in json.dumps(receipts)
            private=Path('\\\\wsl.localhost\\vibeflow-linux'+native_state.root.replace('/','\\'))
            for path in private.rglob('*'):
                if path.is_file():
                    assert secret.encode() not in path.read_bytes(),'Credential persisted in native CLI state'
            checks.append('bound_provider_secret_absent_from_events_and_linux_state')
        result={'passed':True,'count':len(checks),'checks':checks,'actual_cli':True,'runtime':'wsl',
                'provider':'owned_linux_loopback_fixture','hosted_inference':False,'evidence':str(api.root)}
        (api.root/'actual-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result))
    finally:
        control('release')
        (api.root/'turn-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
        (api.root/'provider-calls.json').write_text(json.dumps(control('status')['calls'],indent=2),encoding='utf8')


async def main():
    api=IsolatedServer('cli-wsl-actual',app='actual_turn:app')
    (api.root/'actual_turn.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n",encoding='utf8')
    script=api.root/'provider.py'
    script.write_text('from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\nimport json,threading,uuid,sys\n'+inspect.getsource(Provider)+'\n'+DRIVER,encoding='utf8')
    env=create_isolated_codex_environment(api.root/'provider-home')
    env.update(HOME=str(api.root),TMPDIR=str(api.root))
    provider=CodexStdioClient(['/usr/bin/python3','-I','-S','-u',wsl_path(script)],env,cwd=api.root,distro='vibeflow-linux')
    with NativeStateFixture() as native_state:
        try:
            await provider.start()
            await asyncio.to_thread(exercise,api,provider,asyncio.get_running_loop(),native_state)
        finally:
            await provider.close()
            assert provider.stop_result['verified']
            (api.root/'provider-stop.json').write_text(json.dumps(provider.stop_result,indent=2),encoding='utf8')


if __name__=='__main__':asyncio.run(main())
