"""Actual DeepSeek raw stream vs API SSE vs persisted assistant text.

The observer forwards each real upstream line unchanged. It records only the
isolated generated text and metadata; it never supplies model output.
"""
import argparse
import asyncio
import json
import sys

import httpx

from live_llm_acceptance import FIXTURE,source
from testsupport import IsolatedServer

OBSERVE = '''
observed_lines=httpx.Response.aiter_lines
async def recorded_lines(self,*args,**kwargs):
 parts=[];identifier=None;count=0;size=0
 try:
  async for line in observed_lines(self,*args,**kwargs):
   if self.request.url.host=='api.deepseek.com' and line.startswith('data:'):
    try:data=json.loads(line[5:])
    except ValueError:data={}
    identifier=data.get('id') or identifier
    for choice in data.get('choices') or []:
     content=(choice.get('delta') or {}).get('content')
     if content:
      size+=len(content.encode('utf8'))
      if size>2*1024*1024:raise ValueError('Live text observation exceeds its bound')
      count+=1;parts.append(content)
   yield line
 finally:
  if parts:
   text=''.join(parts)
   credential=self.request.headers.get('Authorization','').removeprefix('Bearer ')
   if credential:text=text.replace(credential,'[REDACTED]')
   with (root/'upstream-text.jsonl').open('a',encoding='utf8') as stream:
    stream.write(json.dumps({'id':identifier,'content':text,'content_chunks':count},ensure_ascii=False)+'\\n')
httpx.Response.aiter_lines=recorded_lines
'''


def verify(configuration,rounds):
    api=IsolatedServer('live-deepseek-stream-integrity',app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(FIXTURE+OBSERVE,encoding='utf8')
    report={'passed':False,'provider':'deepseek','model':configuration['model'],'mock_llm_calls':0,'rounds':[]}
    def save():
        trace=api.root/'upstream.jsonl'
        report['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        report['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in report['upstream'])
        raw=json.dumps(report,ensure_ascii=False,indent=2)
        assert configuration['key'] not in raw
        (api.root/'live-result.json').write_text(raw,encoding='utf8')
    print('LIVE_STREAM_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('Actual stream text preservation')
            profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual stream verification',
                'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
            with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=180) as client:
                for index in range(rounds):
                    conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Streaming text '+str(index),
                        'provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'ask'})
                    events=[]
                    with client.stream('POST',f'/conversations/{conversation["id"]}/messages',json={
                        'content':'不调用工具。请写一段虚构的工程师日记，讲述检查网格和边界条件的过程，分成40行，每行以两位序号开头。约900字。仅纯文本，不使用XML或工具协议标签。',
                        'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line.startswith('data:'):events.append(json.loads(line[5:]))
                            if len(json.dumps(events))>2*1024*1024:raise ValueError('Live SSE evidence exceeds bound')
                    raw=json.dumps(events,ensure_ascii=False,indent=2);assert configuration['key'] not in raw
                    (api.root/f'round-{index}-events.json').write_text(raw,encoding='utf8')
                    upstream=[json.loads(line) for line in (api.root/'upstream-text.jsonl').read_text(encoding='utf8').splitlines()]
                    assert len(upstream)==index+1
                    observed=upstream[-1]
                    received=''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
                    history=api.call('GET',f'/conversations/{conversation["id"]}')
                    messages=[e['payload']['content'] for e in history['events'] if e['type']=='message' and e['payload'].get('role')=='assistant']
                    stored=''.join(messages)
                    item={'conversation_id':conversation['id'],'upstream_id':observed['id'],
                        'upstream_characters':len(observed['content']),'upstream_chunks':observed['content_chunks'],
                        'sse_characters':len(received),'stored_characters':len(stored),
                        'sse_matches_upstream':received==observed['content'],'storage_matches_upstream':stored==observed['content']}
                    report['rounds'].append(item);save()
                    assert any(e.get('type')=='run.completed' for e in events),'Actual model run did not complete'
                    assert item['sse_matches_upstream'] and item['storage_matches_upstream'],'Actual upstream text lost before SSE/persistence'
                    print('LIVE_STREAM_PASS '+str(index)+' chars='+str(len(received)),flush=True)
            report['passed']=True;save()
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1000]}
        save();print('LIVE_STREAM_FAILED '+str(api.root/'live-result.json'),flush=True);return 1
    print('LIVE_STREAM_OK '+str(api.root/'live-result.json'),flush=True);return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--rounds',type=int,default=2);args=parser.parse_args()
    if not 1<=args.rounds<=4:parser.error('Choose one to four real model calls')
    sys.exit(verify(asyncio.run(source(args.source_profile_id)),args.rounds))
