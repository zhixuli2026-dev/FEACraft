"""Actual installed CLI fork and continued native history; local Responses only."""
import io,json,threading,uuid
from http.server import ThreadingHTTPServer
from codex_turn_actual_smoke import Provider
from codex_session_smoke import wait_for
from testsupport import IsolatedServer

class HistoryProvider(Provider):
    inputs=[]
    calls=[]
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']))
        self.inputs.append(json.loads(body).get('input'))
        self.rfile=io.BytesIO(body)
        super().do_POST()

def main():
    provider=ThreadingHTTPServer(('127.0.0.1',0),HistoryProvider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    api=IsolatedServer('cli-fork-actual',app='fork_actual:app')
    (api.root/'fork_actual.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n",encoding='utf8')
    checks=[];receipts=[]
    try:
        with api:
            case=api.create_case('Actual native fork')
            profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'Local fork Responses',
                'base_url':f'http://127.0.0.1:{provider.server_port}/v1','default_model':'fixture-model','api_key':HistoryProvider.secret})
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],'permission_mode':'plan'})
            def turn(cid,text,resume=False):
                session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':cid,
                    'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
                row=api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':text,'client_message_id':uuid.uuid4().hex,'resume':resume})
                value=wait_for(lambda:api.call('GET','/codex/turns/'+row['id']),lambda v:v['phase'] in {'completed','failed'},seconds=50)
                assert value['phase']=='completed' and value['cleanup_verified'],value
                receipts.append(value);return value
            source=turn(conv['id'],'SOURCE_HISTORY_SENTINEL')
            calls=len(HistoryProvider.inputs)
            body={'source_turn_id':source['id'],'client_request_id':uuid.uuid4().hex}
            fork=api.call('POST',f"/codex/conversations/{conv['id']}/fork",body)
            fork=wait_for(lambda:api.call('GET','/codex/forks/'+fork['id']),lambda v:v['phase'] in {'completed','failed','recovery_required'},seconds=50)
            receipts.append(fork)
            assert fork['phase']=='completed' and fork['cleanup_verified'] and fork['thread_id']!=source['thread_id'],fork
            assert len(HistoryProvider.inputs)==calls
            checks.append('actual_persistent_fork_has_distinct_thread_and_no_automatic_inference')
            branch=turn(fork['conversation_id'],'BRANCH_NEW_REQUEST',resume=True)
            assert branch['thread_id']==fork['thread_id']
            inherited=json.dumps(HistoryProvider.inputs[-1],ensure_ascii=False)
            assert 'SOURCE_HISTORY_SENTINEL' in inherited and 'ACTUAL_TURN_OK' in inherited and 'BRANCH_NEW_REQUEST' in inherited
            assert '<conversation_history>' not in inherited
            checks.append('actual_resume_inherits_native_messages_without_text_handoff')
            same=api.call('POST',f"/codex/conversations/{conv['id']}/fork",body)
            assert same['id']==fork['id'] and same['thread_id']==fork['thread_id']
            checks.append('actual_completed_fork_retry_returns_original_branch')
            parent=turn(conv['id'],'PARENT_NEW_REQUEST',resume=True)
            assert parent['thread_id']==source['thread_id']
            parent_input=json.dumps(HistoryProvider.inputs[-1],ensure_ascii=False)
            assert 'PARENT_NEW_REQUEST' in parent_input and 'BRANCH_NEW_REQUEST' not in parent_input
            checks.append('actual_parent_and_branch_histories_diverge_independently')
            assert HistoryProvider.secret not in json.dumps(receipts)
            checks.append('actual_fork_receipts_do_not_expose_provider_credential')
        result={'passed':True,'count':len(checks),'checks':checks,'actual_cli':True,'local_responses_fixture':True,'hosted_inference':False,'evidence':str(api.root)}
        (api.root/'actual-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result))
    finally:
        provider.shutdown();provider.server_close()
        (api.root/'fork-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
        (api.root/'provider-inputs.json').write_text(json.dumps(HistoryProvider.inputs,indent=2),encoding='utf8')

if __name__=='__main__':main()
