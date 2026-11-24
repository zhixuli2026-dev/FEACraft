"""Installed CLI model choices delivered to a local Responses provider."""
import io,json,threading,uuid
from http.server import ThreadingHTTPServer
from codex_turn_actual_smoke import Provider
from codex_session_smoke import wait_for
from testsupport import IsolatedServer

class OptionsProvider(Provider):
    choices=[]
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']));value=json.loads(body)
        self.choices.append({key:value[key] for key in ('model','reasoning','service_tier') if key in value})
        self.rfile=io.BytesIO(body);super().do_POST()

def main():
    provider=ThreadingHTTPServer(('127.0.0.1',0),OptionsProvider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    api=IsolatedServer('native-model-actual',app='model_actual:app')
    (api.root/'model_actual.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n",encoding='utf8')
    checks=[];receipts=[]
    try:
        with api:
            case=api.create_case('Actual native model options')
            profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'Local model Responses',
                'base_url':f'http://127.0.0.1:{provider.server_port}/v1','default_model':'gpt-5.4','api_key':Provider.secret})
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],'model':'gpt-5.4','permission_mode':'approve_writes'})
            def run(cid,choices=None,resume=False):
                session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':cid,
                    'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
                row=api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':'Report fixture answer.',
                    'client_message_id':uuid.uuid4().hex,'resume':resume,'model_options':choices})
                value=wait_for(lambda:api.call('GET','/codex/turns/'+row['id']),lambda v:v['phase'] in {'completed','failed'},seconds=60)
                assert value['phase']=='completed' and value['cleanup_verified'],value
                receipts.append(value);return value
            first=run(conv['id'],{'effort':'high','summary':'detailed','service_tier':'default'})
            assert OptionsProvider.choices[-1]['model']=='gpt-5.4'
            assert OptionsProvider.choices[-1]['reasoning']=={'effort':'high','summary':'detailed'},OptionsProvider.choices
            assert OptionsProvider.choices[-1].get('service_tier') in (None,'default')
            checks.append('actual_provider_receives_model_effort_summary_with_standard_tier')
            second=run(conv['id'],{'effort':'low','summary':'none'},resume=True)
            assert second['thread_id']==first['thread_id']
            assert OptionsProvider.choices[-1]['reasoning'].get('effort')=='low'
            assert OptionsProvider.choices[-1]['reasoning'].get('summary') in (None,'none'),OptionsProvider.choices
            checks.append('actual_resume_accepts_explicit_effort_and_summary_overrides')
            plan=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],'model':'gpt-5.4','permission_mode':'plan'})
            run(plan['id'],{'effort':'high','summary':'concise'})
            assert OptionsProvider.choices[-1]['reasoning']=={'effort':'high','summary':'concise'},OptionsProvider.choices
            checks.append('actual_plan_collaboration_preserves_requested_effort')
            assert Provider.secret not in json.dumps(receipts+OptionsProvider.choices)
        result={'passed':True,'count':len(checks),'checks':checks,'actual_cli':True,'local_responses_fixture':True,
            'hosted_inference':False,'evidence':str(api.root)}
        (api.root/'actual-result.json').write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result))
    finally:
        provider.shutdown();provider.server_close()
        (api.root/'model-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
        (api.root/'provider-choices.json').write_text(json.dumps(OptionsProvider.choices,indent=2),encoding='utf8')

if __name__=='__main__':main()
