"""Native image delivery through the product API and the installed Windows CLI."""
import base64,hashlib,json,random,struct,threading,uuid,zlib
from http.server import ThreadingHTTPServer
from pathlib import Path
from testsupport import IsolatedServer
from codex_fork_actual_smoke import HistoryProvider
from codex_session_smoke import wait_for

def fixture_image():
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    raw=random.Random(17).randbytes(1000*1000*3)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1000,1000,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(b'\0'+raw[n:n+3000] for n in range(0,len(raw),3000))))+chunk(b'IEND',b'')

def main():
    provider=ThreadingHTTPServer(('127.0.0.1',0),HistoryProvider)
    threading.Thread(target=provider.serve_forever,daemon=True).start()
    api=IsolatedServer('native-image-actual',app='image_actual:app')
    (api.root/'image_actual.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n",encoding='utf8')
    content=fixture_image();checks=[];receipts=[]
    try:
        with api:
            case=api.create_case('Actual native image')
            profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'Local image Responses','base_url':f'http://127.0.0.1:{provider.server_port}/v1','default_model':'fixture-model','api_key':HistoryProvider.secret})
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile['id'],'permission_mode':'plan'})
            def turn(text,images=None,resume=False):
                session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
                body={'content':text,'client_message_id':uuid.uuid4().hex,'images':images or [],'resume':resume}
                row=api.call('POST',f"/codex/sessions/{session['id']}/turns",body)
                value=wait_for(lambda:api.call('GET','/codex/turns/'+row['id']),lambda v:v['phase'] in {'completed','failed'},seconds=60)
                assert value['phase']=='completed' and value['cleanup_verified'],value
                receipts.append(value);return value
            first=turn('',[{'name':'fixture.png','mime':'image/png','data':base64.b64encode(content).decode()}])
            images=[part for item in HistoryProvider.inputs[-1] for part in (item.get('content') or []) if isinstance(part,dict) and part.get('type')=='input_image']
            assert len(images)==1 and base64.b64decode(images[0]['image_url'].split(',',1)[1])==content
            checks.append('actual_native_model_receives_exact_three_megabyte_image_from_product_api')
            history=api.call('GET',f"/conversations/{conv['id']}")['events']
            metadata=next(e for e in history if e['payload'].get('role')=='user')['payload']['images'][0]
            assert api.call('GET',f"/cases/{case['id']}/raw?path={metadata['path']}",raw=True)==content
            checks.append('actual_image_history_reloads_from_case_file_without_inline_bytes')
            second=turn('Continue using the earlier image.',resume=True)
            assert second['thread_id']==first['thread_id']
            images=[part for item in HistoryProvider.inputs[-1] for part in (item.get('content') or []) if isinstance(part,dict) and part.get('type')=='input_image']
            assert len(images)==1 and base64.b64decode(images[0]['image_url'].split(',',1)[1])==content
            checks.append('actual_resumed_thread_retains_image_after_previous_scope_closes')
            assert HistoryProvider.secret not in json.dumps(receipts)
            checks.append('actual_image_receipts_do_not_include_provider_secret')
        result={'passed':True,'count':len(checks),'checks':checks,'image_bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),'actual_cli':True,'local_responses_fixture':True,'hosted_inference':False,'evidence':str(api.root)}
        (api.root/'actual-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result))
    finally:
        provider.shutdown();provider.server_close()
        (api.root/'turn-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')

if __name__=='__main__':main()
