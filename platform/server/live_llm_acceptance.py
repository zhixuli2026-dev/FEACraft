"""Explicit, paid live provider acceptance through actual platform API routes.

Reads one existing DeepSeek profile without changing the developer database.
No fake model outputs. Credentials stay in memory or isolated encrypted vault.
"""
import argparse,asyncio,json,os,sys,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from testsupport import IsolatedServer,SERVER

FIXTURE='''
import json,time
from pathlib import Path
import httpx
from app.services import llm
root=Path(__file__).parent
llm._VAULT_KEY_PATH=root/'.vault_key'
from app.main import app
calls=0
original_send=httpx.AsyncClient.send
def receipt(value):
 with (root/'upstream.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(value,ensure_ascii=False)+'\\n')
async def sending(self,request,*args,**kwargs):
 global calls
 live=request.url.host=='api.deepseek.com'
 if live:
  calls+=1
  if calls>globals().get('live_request_limit',8):raise RuntimeError('Live acceptance exceeded its request limit')
  started=time.monotonic()
 try:
  response=await original_send(self,request,*args,**kwargs)
 except httpx.TransportError as error:
  if live:receipt({'kind':'transport_error','host':request.url.host,'path':request.url.path,
                   'error_type':type(error).__name__,'elapsed_ms':round((time.monotonic()-started)*1000)})
  raise
 if live:
  record={'kind':'http','host':request.url.host,'path':request.url.path,'status':response.status_code,
   'elapsed_ms':round((time.monotonic()-started)*1000),'request_id':response.headers.get('x-request-id'),
   'date':response.headers.get('date')}
  if response.is_stream_consumed and response.status_code==200:
   data=response.json()
   record.update({key:data[key] for key in ('id','model','usage') if key in data})
  receipt(record)
 return response
httpx.AsyncClient.send=sending
original_lines=httpx.Response.aiter_lines
async def lines(self,*args,**kwargs):
 observed={}
 try:
  async for line in original_lines(self,*args,**kwargs):
   if self.request.url.host=='api.deepseek.com' and line.startswith('data:'):
    try:
     data=json.loads(line[5:])
     for key in ('id','model','usage'):
      if data.get(key) is not None:observed[key]=data[key]
    except (ValueError,AttributeError):pass
   yield line
 finally:
  if observed:receipt({'kind':'stream_identity',**observed})
httpx.Response.aiter_lines=lines
'''


async def source(profile_id):
 from sqlalchemy.ext.asyncio import create_async_engine
 from sqlalchemy import text
 from app.config import Settings
 from app.services.provider_vault import read as read_vault
 settings=Settings(_env_file=SERVER/'.env')
 engine=create_async_engine(settings.database_url)
 try:
  async with engine.connect() as db:
   row=(await db.execute(text('SELECT provider,base_url,default_model,key_ciphertext FROM provider_profiles WHERE id=:id'),{'id':profile_id})).mappings().one()
   assert row['provider']=='deepseek' and urlsplit(row['base_url']).hostname=='api.deepseek.com','Only the authorized official DeepSeek profile is accepted'
   path=SERVER/Path(settings.vault_key_path).expanduser() if settings.vault_key_path else SERVER/'app/.vault_key'
   key=read_vault(path).decrypt(row['key_ciphertext'].encode()).decode()
   return {'provider':row['provider'],'base_url':row['base_url'],'model':row['default_model'],'key':key}
 finally:await engine.dispose()


def verify(configuration, *, receipt=None):
 api=IsolatedServer('live-deepseek',app='live_fixture:app');(api.root/'live_fixture.py').write_text(FIXTURE,encoding='utf8')
 result={'kind':'real_llm_acceptance','simulated_llm':False,'source_database_modified':False,'passed':False,'checks':[]}
 def save():
  trace=api.root/'upstream.jsonl'
  result['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
  (api.root/'live-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
  if receipt:
   fields = ('kind','passed','simulated_llm','source_database_modified','model','provider','host','checks','upstream')
   receipt.write_text(json.dumps({field:result[field] for field in fields if field in result},indent=2)+'\n',
                      encoding='utf8')
 print('LIVE_LLM_EVIDENCE '+str(api.root),flush=True)
 try:
  with api:
   case=api.create_case('Real DeepSeek acceptance')
   profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Real DeepSeek acceptance',
    'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
   assert configuration['key'] not in json.dumps(profile)
   with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=180) as client:
    models=client.get('/llm/models',params={'provider_profile_id':profile['id']});models.raise_for_status();models=models.json()
    assert not models['fallback'],models
    available=[row['id'] for row in models['models']]
    assert configuration['model'] in available,{'configured_model':configuration['model'],'available_models':available}
    result.update(model=configuration['model'],provider='deepseek',host='api.deepseek.com',available_models=available)
    result['checks'].append('actual_models_endpoint');save();print('LIVE_PASS models '+configuration['model'],flush=True)
    translated=client.post('/llm/translate',json={'text':'The inlet velocity is 2 m/s.','target':'zh','provider_profile_id':profile['id']})
    translated.raise_for_status();translated=translated.json();assert translated['ok'] and '2' in translated['translation']
    result['translation']=translated['translation'];result['checks'].append('actual_nonstream_translation');save();print('LIVE_PASS translation',flush=True)
    nonce='LIVE_'+uuid.uuid4().hex
    api.put('Ref/live-proof.txt',nonce)
    for mode,prompt in [('ask','请只用一句中文回答：有限体积法为什么强调守恒？不要调用工具，控制在40字以内。'),
      ('plan','请调用 read_text_file 工具读取 Ref/live-proof.txt，只使用这个工具。拿到内容后原样回复其中的验证字符串；不要猜测，不要修改文件。')]:
     conversation=api.call('POST','/conversations',{'case_id':case['id'],'title':'Live '+mode,'provider_profile_id':profile['id'],
       'model':configuration['model'],'permission_mode':mode})
     events=[];started=time.monotonic()
     with client.stream('POST',f'/conversations/{conversation["id"]}/messages',json={'content':prompt,'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
      response.raise_for_status()
      for line in response.iter_lines():
       if time.monotonic()-started>180:raise TimeoutError('Live model response exceeded180 seconds')
       if line.startswith('data:'):events.append(json.loads(line[5:]))
       if len(json.dumps(events))>2*1024*1024:raise ValueError('Live evidence exceeded2MiB')
     assert configuration['key'] not in json.dumps(events)
     (api.root/(mode+'-events.json')).write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf8')
     failures=[e for e in events if e.get('type') in {'stream.error','request.failed','request.timed_out'}]
     assert not failures,failures
     assert any(e.get('type')=='message.completed' for e in events),[e.get('type') for e in events]
     assert any(e.get('type')=='run.completed' for e in events),[e.get('type') for e in events]
     answer=''.join(e.get('text','') for e in events if e.get('type')=='text.delta');assert answer.strip()
     saved=api.call('GET',f'/conversations/{conversation["id"]}')
     assert any(e['type']=='message' and e['payload'].get('role')=='assistant' for e in saved['events'])
     if mode=='plan':
      assert any(e.get('type')=='tool.result' for e in events),[e.get('type') for e in events]
      assert nonce in answer,answer
      assert any(e['type']=='tool_result' and nonce in str(e['payload']) for e in saved['events'])
     result[mode]={'conversation_id':conversation['id'],'answer':answer,'event_types':[e.get('type') for e in events],
       'elapsed_seconds':round(time.monotonic()-started,2),'persisted':True}
     result['checks'].append('actual_stream_and_persistence' if mode=='ask' else 'actual_tool_read_and_followup');save();print('LIVE_PASS '+mode,flush=True)
   result['passed']=True;save()
   assert sum(r['kind']=='http' and r['path'].endswith('/chat/completions') and r['status']==200 for r in result['upstream'])>=4
 except BaseException as error:
  result['passed']=False;result['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]};save()
  print('LIVE_LLM_FAILED '+json.dumps(result['failure'],ensure_ascii=False),flush=True);return 1
 print('LIVE_LLM_OK '+str(api.root/'live-result.json'),flush=True);return 0


if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 credential=parser.add_mutually_exclusive_group(required=True)
 credential.add_argument('--source-profile-id',type=int)
 credential.add_argument('--credential-env',action='store_true',
                         help='Use the dedicated VIBEFLOW_REPLAY_DEEPSEEK_KEY CI secret')
 parser.add_argument('--receipt',type=Path,help='Write check and provider receipt metadata without answer text')
 args=parser.parse_args()
 if args.credential_env:
  key=os.environ.pop('VIBEFLOW_REPLAY_DEEPSEEK_KEY','').strip()
  if not key:
   parser.error('VIBEFLOW_REPLAY_DEEPSEEK_KEY is required for the authorized nightly replay')
  configuration={'provider':'deepseek','base_url':'https://api.deepseek.com/v1','model':'deepseek-flash','key':key}
 else:
  configuration=asyncio.run(source(args.source_profile_id))
 sys.exit(verify(configuration,receipt=args.receipt))
