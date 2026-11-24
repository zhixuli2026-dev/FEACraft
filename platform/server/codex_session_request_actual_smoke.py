"""Installed Windows CLI started once by durable admission; local Responses only."""
import json,threading,uuid
from http.server import ThreadingHTTPServer
from codex_turn_actual_smoke import Provider
from codex_session_smoke import wait_for
from testsupport import IsolatedServer


def main():
    provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    api=IsolatedServer('native-startup-actual',app='startup_actual:app')
    (api.root/'startup_actual.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n",encoding='utf8')
    checks=[];receipts=[]
    try:
        with api:
            case=api.create_case('Actual native startup')
            profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'Local startup Responses',
                'base_url':f'http://127.0.0.1:{provider.server_port}/v1','default_model':'gpt-5.4','api_key':Provider.secret})
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],
                'model':'gpt-5.4','permission_mode':'approve_writes'})
            endpoint=f"/codex/conversations/{conv['id']}/session-requests"
            def start(purpose='chat'):
                body={'client_request_id':uuid.uuid4().hex,'provider_profile_id':profile['id'],
                    'provider_wire_api':'responses','runtime':'windows','purpose':purpose}
                value=api.call('POST',endpoint,body)
                value=wait_for(lambda:api.call('GET','/codex/session-requests/'+value['id']),
                    lambda v:v['phase'] not in {'admitted','starting'},seconds=60)
                assert value['phase']=='completed' and value['session']['phase']=='active',value
                assert api.call('POST',endpoint,body)['id']==value['id']
                assert api.call('GET',endpoint+'?client_request_id='+body['client_request_id'])[0]['id']==value['id']
                receipts.append(value);return value['session']
            def turn(session,resume=False):
                value=api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':'Report fixture answer.',
                    'client_message_id':uuid.uuid4().hex,'resume':resume})
                value=wait_for(lambda:api.call('GET','/codex/turns/'+value['id']),lambda v:v['phase'] in {'completed','failed'},seconds=60)
                assert value['phase']=='completed' and value['cleanup_verified'],value
                receipts.append(value);return value
            session=start('inspect')
            account=api.call('GET',f"/codex/sessions/{session['id']}/account")
            assert account['credential']['inference_configured']
            closed=api.call('DELETE','/codex/sessions/'+session['id']);assert closed['cleanup_verified']
            checks.append('installed_cli_metadata_startup_is_discoverable_idempotent_and_closes_verified')
            first=turn(start());checks.append('installed_cli_admitted_startup_hands_off_to_explicit_message')
            second=turn(start(),resume=True);assert second['thread_id']==first['thread_id']
            checks.append('new_admitted_cli_scope_resumes_native_thread_after_previous_verified_close')
            final=api.call('GET',endpoint)
            assert len(final)==3 and all(v['phase']=='completed' and v['cleanup_verified'] for v in final)
            assert Provider.secret not in json.dumps(receipts+final)
        result={'passed':True,'count':len(checks),'checks':checks,'actual_cli':True,'local_responses_fixture':True,
            'hosted_inference':False,'evidence':str(api.root)}
        (api.root/'actual-result.json').write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result))
    finally:
        provider.shutdown();provider.server_close()
        (api.root/'startup-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')


if __name__=='__main__':main()
