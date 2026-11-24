"""Explicit actual provider verification of exact tool readback and JSON format."""
import argparse,asyncio,json,uuid
import httpx
from live_llm_acceptance import FIXTURE,source
from testsupport import IsolatedServer

def verify(configuration):
    api=IsolatedServer('live-output-format',app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(FIXTURE,encoding='utf8')
    report={'passed':False,'simulated_llm':False,'model':configuration['model'],
        'host':'api.deepseek.com','checks':[],'llm_calls':0,'source_database_modified':False}
    def save():
        trace=api.root/'upstream.jsonl'
        report['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in report['upstream'])
        raw=json.dumps(report,ensure_ascii=False,indent=2)
        assert configuration['key'] not in raw
        (api.root/'live-result.json').write_text(raw,encoding='utf8')
    try:
        with api:
            api.create_case('Actual model output constraints')
            profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Real output format acceptance',
                'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
            nonce='EXACT_'+uuid.uuid4().hex;api.put('Ref/exact-output.txt',nonce+'\n')
            cases=[('plan','请调用 read_text_file 一次读取 Ref/exact-output.txt，随后原样回复文件中 EXACT_ 开头的字符串。最终只输出该字符串，不要前言、解释、状态汇报或代码块；不要猜测或修改文件。'),
                   ('ask','请严格只输出 JSON 对象 {"status":"ok"}，不添加 Markdown 围栏、前言或状态汇报。')]
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=180) as client:
                for mode,prompt in cases:
                    conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Exact '+mode,
                        'provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':mode})
                    events=[]
                    with client.stream('POST',f'/conversations/{conversation["id"]}/messages',json={
                        'content':prompt,'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line.startswith('data:'):events.append(json.loads(line[5:]))
                            if len(json.dumps(events))>2*1024*1024:raise ValueError('Live evidence exceeds bound')
                    raw=json.dumps(events,ensure_ascii=False,indent=2);assert configuration['key'] not in raw
                    (api.root/(mode+'-events.json')).write_text(raw,encoding='utf8')
                    answer=''.join(v.get('text','') for v in events if v.get('type')=='text.delta')
                    report[mode]={'conversation_id':conversation['id'],'answer':answer};save()
                    failures=[v for v in events if v.get('type') in {'request.failed','request.timed_out','stream.error'}]
                    assert not failures,failures
                    assert any(v.get('type')=='run.completed' for v in events)
                    if mode=='plan':
                        tools=[v for v in events if v.get('type')=='tool.result']
                        assert len(tools)==1 and tools[0]['tool']=='read_text_file' and tools[0]['ok'] and nonce in tools[0]['result']
                        assert answer==nonce,'Actual model did not preserve the exact requested format'
                        assert api.call('GET',api.prefix+'/raw?path=Ref/exact-output.txt',raw=True)==(nonce+'\n').encode()
                    else:
                        assert json.loads(answer)=={'status':'ok'} and answer.strip().startswith('{')
                        assert not any(v.get('type')=='tool.result' for v in events)
                    history=api.call('GET',f'/conversations/{conversation["id"]}')
                    assert any(v['type']=='message' and v['payload'].get('role')=='assistant'
                               and v['payload'].get('content')==answer for v in history['events'])
                    report['checks'].append('exact_tool_readback' if mode=='plan' else 'json_without_workflow_footer')
                    save();print('LIVE_FORMAT_PASS '+mode,flush=True)
            successful=[v for v in report['upstream'] if v['kind']=='http' and v['path'].endswith('/chat/completions') and v['status']==200]
            assert len(successful)>=3
            report['passed']=True;save()
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]}
        save();print('LIVE_FORMAT_FAILED '+str(api.root/'live-result.json'),flush=True);return 1
    finally:
        (api.root / '.vault_key').unlink(missing_ok=True)
    print('LIVE_FORMAT_OK '+str(api.root/'live-result.json'),flush=True);return 0

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);args=parser.parse_args()
    raise SystemExit(verify(asyncio.run(source(args.source_profile_id))))
