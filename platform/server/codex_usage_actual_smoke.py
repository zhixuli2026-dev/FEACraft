"""Installed CLI usage notifications via product API and local Responses only."""
import json,threading,uuid
from http.server import ThreadingHTTPServer
from codex_turn_actual_smoke import Provider
from codex_session_smoke import wait_for
from testsupport import IsolatedServer

def main():
    provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    api=IsolatedServer('native-usage-actual',app='usage_actual:app')
    (api.root/'usage_actual.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n",encoding='utf8')
    checks=[];receipts=[]
    try:
        with api:
            case=api.create_case('Actual native usage')
            profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'Local usage Responses',
                'base_url':f'http://127.0.0.1:{provider.server_port}/v1','default_model':'fixture-model','api_key':Provider.secret})
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],'permission_mode':'plan'})
            def run(resume=False):
                session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],
                    'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
                row=api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':'Report fixture answer.',
                    'client_message_id':uuid.uuid4().hex,'resume':resume})
                value=wait_for(lambda:api.call('GET','/codex/turns/'+row['id']),lambda v:v['phase'] in {'completed','failed'},seconds=60)
                assert value['phase']=='completed' and value['cleanup_verified'],value
                receipts.append(value);return value
            first=run()
            assert first['usage']['status']=='reported' and first['usage']['last']['totalTokens']==6,first
            assert first['usage']['last']['inputTokens']==1 and first['usage']['last']['outputTokens']==5
            checks.append('actual_cli_reports_provider_counters_through_durable_api')
            second=run(resume=True)
            assert second['thread_id']==first['thread_id']
            assert second['usage']['last']['totalTokens']==6 and second['usage']['total']['totalTokens']==12,second
            checks.append('actual_resume_distinguishes_last_call_from_inherited_thread_total')
            first_again=api.call('GET','/codex/turns/'+first['id'])
            assert first_again['usage']==first['usage']
            stream=api.call('GET','/codex/turns/'+second['id']+'/events',raw=True).decode()
            assert 'event: usage.updated' in stream
            assert Provider.secret not in stream+json.dumps(receipts)
            checks.append('closed_scopes_keep_prior_snapshot_and_replay_without_secrets')
        result={'passed':True,'count':len(checks),'checks':checks,'actual_cli':True,'local_responses_fixture':True,
            'hosted_inference':False,'evidence':str(api.root)}
        (api.root/'actual-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result))
    finally:
        provider.shutdown();provider.server_close()
        (api.root/'usage-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')

if __name__=='__main__':main()
