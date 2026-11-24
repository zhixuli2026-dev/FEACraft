"""Actual WSL image delivery, native resume and fork via local Linux Responses."""
import asyncio,base64,hashlib,inspect,json,uuid
from testsupport import IsolatedServer
from codex_session_smoke import wait_for
from codex_turn_actual_smoke import Provider
from codex_image_actual_smoke import fixture_image
from codex_wsl_test_support import NativeStateFixture
from app.services.codex_stdio import CodexStdioClient
from app.services.codex_environment import create_isolated_codex_environment
from app.services.owned_process import wsl_path

DRIVER='''
class Images(Provider):
    seen=[]
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']))
        message=json.loads(body)
        parts=[p for e in message['input'] for p in (e.get('content') or []) if isinstance(p,dict)]
        self.seen.append({'images':[hashlib.sha256(base64.b64decode(p['image_url'].split(',',1)[1])).hexdigest() for p in parts if p.get('type')=='input_image'],
            'markers':[p['text'] for p in parts if p.get('type')=='input_text' and p.get('text','').startswith('IMAGE_')]})
        self.rfile=io.BytesIO(body)
        super().do_POST()
provider=ThreadingHTTPServer(('127.0.0.1',0),Images)
threading.Thread(target=provider.serve_forever,daemon=True).start()
for line in sys.stdin:
    value=json.loads(line);rid=value.get('id');method=value.get('method')
    if method=='initialized':continue
    if method=='initialize':result={'userAgent':'linux-image-fixture','port':provider.server_port,'secret':Images.secret}
    elif method=='status':result={'seen':Images.seen,'calls':len(Images.calls)}
    else:raise ValueError('Unknown fixture operation')
    print(json.dumps({'id':rid,'result':result}),flush=True)
'''

def exercise(api,provider,loop):
    def observed():return asyncio.run_coroutine_threadsafe(provider.request('status'),loop).result(15)
    content=fixture_image();digest=hashlib.sha256(content).hexdigest();checks=[];receipts=[]
    with api:
        case=api.create_case('WSL native images')
        profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'Linux local image fixture','base_url':f"http://127.0.0.1:{provider.initialize_result['port']}/v1",'default_model':'fixture-model','api_key':provider.initialize_result['secret']})
        conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],'permission_mode':'plan'})
        def turn(cid,text,images=None,resume=False):
            session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':cid,'provider_profile_id':profile['id'],'provider_wire_api':'responses','runtime':'wsl'})
            value=api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':text,'images':images or [],'client_message_id':uuid.uuid4().hex,'resume':resume})
            value=wait_for(lambda:api.call('GET','/codex/turns/'+value['id']),lambda v:v['phase'] in {'completed','failed'},seconds=60)
            assert value['phase']=='completed' and value['cleanup_verified'],value
            scope=api.call('GET','/codex/sessions/'+session['id'])
            assert scope['stop']['native_state']=='exited' and scope['process']['root']['native']['process']['uid']==65534
            assert observed()['seen'][-1]['images']==[digest]
            receipts.append(value);return value
        first=turn(conv['id'],'IMAGE_SOURCE',[{'name':'fixture.png','mime':'image/png','data':base64.b64encode(content).decode()}])
        checks.append('actual_wsl_nonroot_cli_reads_owned_mounted_image_and_delivers_exact_bytes')
        second=turn(conv['id'],'IMAGE_RESUME',resume=True)
        assert first['thread_id']==second['thread_id']
        checks.append('actual_wsl_resume_preserves_image_after_prior_scope_closes')
        before=observed()['calls']
        fork=api.call('POST',f"/codex/conversations/{conv['id']}/fork",{'source_turn_id':second['id'],'client_request_id':uuid.uuid4().hex})
        fork=wait_for(lambda:api.call('GET','/codex/forks/'+fork['id']),lambda v:v['phase'] in {'completed','failed','recovery_required'},seconds=60)
        assert fork['phase']=='completed' and fork['cleanup_verified'] and fork['runtime']=='wsl',fork
        assert observed()['calls']==before
        receipts.append(fork);checks.append('actual_wsl_fork_creates_distinct_native_thread_without_inference')
        branch=turn(fork['conversation_id'],'IMAGE_BRANCH',resume=True)
        assert branch['thread_id']==fork['thread_id'] and branch['thread_id']!=first['thread_id']
        assert 'IMAGE_SOURCE' in observed()['seen'][-1]['markers']
        checks.append('actual_wsl_fork_inherits_image_and_source_user_context')
        turn(conv['id'],'IMAGE_PARENT',resume=True)
        assert 'IMAGE_BRANCH' not in observed()['seen'][-1]['markers']
        checks.append('actual_wsl_parent_history_excludes_branch_messages')
    result={'passed':True,'count':len(checks),'checks':checks,'image_bytes':len(content),'actual_cli':True,'runtime':'wsl','local_responses_fixture':True,'hosted_inference':False,'evidence':str(api.root)}
    (api.root/'actual-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    (api.root/'receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    print(json.dumps(result))

async def main():
    api=IsolatedServer('native-image-wsl',app='image_actual:app')
    (api.root/'image_actual.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n",encoding='utf8')
    script=api.root/'provider.py'
    script.write_text('from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\nimport json,threading,uuid,sys,io,base64,hashlib\n'+inspect.getsource(Provider)+'\n'+DRIVER,encoding='utf8')
    env=create_isolated_codex_environment(api.root/'provider-home');env.update(HOME=str(api.root),TMPDIR=str(api.root))
    provider=CodexStdioClient(['/usr/bin/python3','-I','-S','-u',wsl_path(script)],env,cwd=api.root,distro='vibeflow-linux')
    with NativeStateFixture():
        try:
            await provider.start()
            await asyncio.to_thread(exercise,api,provider,asyncio.get_running_loop())
        finally:
            await provider.close();assert provider.stop_result['verified']
            (api.root/'provider-stop.json').write_text(json.dumps(provider.stop_result,indent=2),encoding='utf8')

if __name__=='__main__':asyncio.run(main())
