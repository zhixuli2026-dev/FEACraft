"""Actual upstream text, streamed text and durable messages must all match."""
import argparse,asyncio,json,re,uuid
import httpx
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from testsupport import IsolatedServer

FIXTURE=LIVE_FIXTURE+'''
_raw_provider_lines=httpx.Response.aiter_lines
async def _record_provider_text(self,*args,**kwargs):
 async for line in _raw_provider_lines(self,*args,**kwargs):
  if self.request.url.host=='api.deepseek.com' and line.startswith('data:'):
   try:value=json.loads(line[5:])
   except ValueError:value={}
   for choice in value.get('choices',[]):
    text=choice.get('delta',{}).get('content')
    if text:
     with (root/'raw-provider-text.jsonl').open('a',encoding='utf8') as output:
      output.write(json.dumps({'id':value.get('id'),'text':text},ensure_ascii=False)+'\\n')
  yield line
httpx.Response.aiter_lines=_record_provider_text
'''


def verify(configuration):
    api=IsolatedServer('live-whole-turn-format',app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(FIXTURE,encoding='utf8')
    report={'passed':False,'simulated_llm':False,'source_database_modified':False,'provider':'deepseek',
        'host':'api.deepseek.com','model':configuration['model'],'cases':[],'llm_calls':0,'checks':[]}
    def raw_rows():
        path=api.root/'raw-provider-text.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []
    def save():
        path=api.root/'upstream.jsonl'
        report['upstream']=[json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []
        report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in report['upstream'])
        encoded=json.dumps(report,ensure_ascii=False,indent=2)
        assert configuration['key'] not in encoded
        (api.root/'live-result.json').write_text(encoded,encoding='utf8')
    print('LIVE_WHOLE_TURN_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('Actual whole-turn format')
            profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual whole-turn format',
                'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
            recovery_prompt='请调用 read_text_file 读取 Ref/retirement.txt，然后原样回复文件里 RETIREMENT_ 开头的完整字符串。仅调用这个工具一次，不修改文件，不猜测；拿到结果后直接回答。'
            cases=[('recovery_'+str(i),'plan',recovery_prompt) for i in range(3)]+[
                ('json','ask','请严格只输出 JSON 对象 {"status":"ok"}，不添加 Markdown 围栏、前言或状态汇报。'),
                ('explanation','ask','请用两句中文解释为什么有限体积法适合做守恒检查。不要调用工具，不要输出 JSON。')]
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
                for name,mode,prompt in cases:
                    proof='RETIREMENT_'+uuid.uuid4().hex
                    api.put('Ref/retirement.txt',proof+'\n')
                    conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':name,
                        'permission_mode':mode,'provider_profile_id':profile['id'],'model':configuration['model']})
                    start=len(raw_rows());events=[]
                    with client.stream('POST',f'/conversations/{conversation["id"]}/messages',json={
                        'content':prompt,'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line.startswith('data:'):events.append(json.loads(line[5:]))
                            if len(json.dumps(events))>2*1024*1024:raise ValueError('Bounded evidence exceeded')
                    encoded=json.dumps(events,ensure_ascii=False,indent=2);assert configuration['key'] not in encoded
                    (api.root/(name+'-events.json')).write_text(encoded,encoding='utf8')
                    answer=''.join(v.get('text','') for v in events if v.get('type')=='text.delta')
                    upstream=raw_rows()[start:]
                    raw_answer=''.join(v['text'] for v in upstream)
                    history=api.call('GET',f'/conversations/{conversation["id"]}')
                    messages=[v['payload'].get('content') or '' for v in history['events']
                        if v['type']=='message' and v['payload'].get('role')=='assistant']
                    case={'name':name,'conversation_id':conversation['id'],'answer':answer,'raw_upstream_text':raw_answer,
                        'persisted_assistant_messages':messages,'passed':False,'tools':[v.get('tool') for v in events if v.get('type')=='tool.result']}
                    report['cases'].append(case);save()
                    assert not [v for v in events if v.get('type') in {'request.failed','request.timed_out','stream.error'}]
                    assert any(v.get('type')=='run.completed' for v in events)
                    assert answer==raw_answer==''.join(messages),'Visible text was changed or persistence differs'
                    if mode=='plan':
                        tools=[v for v in events if v.get('type')=='tool.result']
                        assert len(tools)==1 and tools[0]['tool']=='read_text_file' and tools[0]['ok'] and proof in tools[0]['result']
                        case['strict_answer_only_proof']=answer==proof
                        save();assert case['strict_answer_only_proof'],'Model emitted extra text in the whole tool turn'
                        assert api.call('GET',api.prefix+'/raw?path=Ref/retirement.txt',raw=True)==(proof+'\n').encode()
                    elif name=='json':
                        assert json.loads(answer)=={'status':'ok'} and not case['tools']
                    else:
                        assert answer.strip() and '守恒' in answer and not case['tools'] and not answer.lstrip().startswith('{')
                        assert len([v for v in re.split(r'[。！？!?]+',answer) if v.strip()])==2
                    case['passed']=True;report['checks'].append(name);save()
                    print('LIVE_WHOLE_TURN_PASS '+name,flush=True)
            successful=[v for v in report['upstream'] if v['kind']=='http' and v['status']==200 and v['path'].endswith('/chat/completions')]
            assert len(successful)==8 and report['llm_calls']==8
            report['passed']=True;save()
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]}
        save();print('LIVE_WHOLE_TURN_FAILED '+str(api.root/'live-result.json'),flush=True);return 1
    print('LIVE_WHOLE_TURN_OK '+str(api.root/'live-result.json'),flush=True);return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);args=parser.parse_args()
    raise SystemExit(verify(asyncio.run(source(args.source_profile_id))))
