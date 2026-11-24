"""Actual deployed Linux Codex session/turn via the production PG/S3 API."""
import argparse,asyncio,hashlib,json,secrets,time,uuid
from pathlib import Path
import httpx
from live_llm_acceptance import source
ROOT=Path(__file__).resolve().parents[2]

def run(cfg):
 folder=ROOT/'tmp'/('live-linux-managed-codex-'+secrets.token_hex(6));folder.mkdir();(folder/'private').mkdir()
 result={'passed':False,'mock_llm_calls':0,'provider':'deepseek','model':cfg['model'],'checks':[]};session=None
 def save(name,value):
  raw=json.dumps(value,ensure_ascii=False,indent=2)+'\n';assert cfg['key'] not in raw;(folder/name).write_text(raw,encoding='utf8')
 print('MANAGED_CODEX_EVIDENCE '+str(folder),flush=True)
 with httpx.Client(base_url='http://127.0.0.1:57700/api/v1',trust_env=False,timeout=180) as client:
  def call(method,path,**kwargs):
   deadline=time.monotonic()+45
   while True:
    response=client.request(method,path,**kwargs)
    if response.status_code==409:
     detail=response.json().get('detail')
     if isinstance(detail,dict) and detail.get('code')=='storage_quota_busy' and time.monotonic()<deadline:time.sleep(.3);continue
    assert response.status_code in (200,201,202),(path,response.status_code,response.text[:500].replace(cfg['key'],'[redacted]'))
    return response.json()
  try:
   account={'email':'linux-codex-'+secrets.token_hex(8)+'@example.com','password':secrets.token_hex(24)};save('private/account.json',account)
   client.headers['Authorization']='Bearer '+call('POST','/auth/register',json={**account,'name':'Linux managed Codex acceptance'})['token']
   workspace=call('GET','/workspaces')[0]['id'];case=call('POST',f'/workspaces/{workspace}/cases',json={'name':'Real Linux managed Codex'})
   result['case_id']=case['id'];prefix='/cases/'+str(case['id'])
   proof='MANAGED_LINUX_CODEX_'+secrets.token_hex(16);call('PUT',prefix+'/file',json={'path':'Ref/proof.txt','content':proof+'\n'})
   profile=call('POST','/provider-profiles',json={'provider':'deepseek','name':'Authorized Linux Codex','base_url':cfg['base_url'],'default_model':cfg['model'],'api_key':cfg['key']})
   conversation=call('POST','/conversations',json={'case_id':case['id'],'title':'Real Linux Codex file read','provider_profile_id':profile['id'],'model':cfg['model'],'permission_mode':'full_access'})
   result['conversation_id']=conversation['id'];save('result.json',result)
   caps=call('GET','/runtime/capabilities');save('capabilities.json',caps)
   assert all(name in caps['cli_agents'] for name in ('codex','opencode','kimi')) and caps['stage_checks']['V']['ok']
   session=call('POST','/codex/sessions',json={'case_id':case['id'],'conversation_id':conversation['id'],'provider_profile_id':profile['id'],'provider_wire_api':'responses'})
   result['session_id']=session['id'];save('session.json',session);save('result.json',result)
   turn=call('POST','/codex/sessions/'+session['id']+'/turns',json={'content':'Read Ref/proof.txt by actually executing cat Ref/proof.txt, then reply only with the exact text. Do not modify any files.','client_message_id':uuid.uuid4().hex,'timeout_seconds':120})
   result['turn_id']=turn['id'];save('result.json',result)
   deadline=time.monotonic()+170
   while turn['phase'] not in {'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}:
    if time.monotonic()>=deadline:raise TimeoutError('Observation deadline; retain the original turn for reconciliation')
    time.sleep(.5);turn=call('GET','/codex/turns/'+turn['id'])
   save('turn.json',turn)
   response=client.get('/codex/turns/'+turn['id']+'/events');response.raise_for_status();assert cfg['key'] not in response.text
   (folder/'events.sse').write_text(response.text,encoding='utf8')
   events=[json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
   answer=''.join(e.get('payload',{}).get('text','') for e in events if e.get('type')=='text.delta')
   commands=[e['payload'] for e in events if e.get('type')=='item.completed' and e.get('payload',{}).get('type')=='commandExecution']
   result.update(answer=answer,commands=commands)
   assert turn['phase']=='completed' and turn['native_status']=='completed' and turn['cleanup_verified']
   assert proof in answer and any(c.get('exitCode')==0 and proof in c.get('aggregatedOutput','') for c in commands)
   history=call('GET','/conversations/'+str(conversation['id']));save('conversation.json',history)
   assert any(e['type']=='message' and e['payload'].get('role')=='assistant' and proof in e['payload'].get('content','') for e in history['events'])
   response=client.get(prefix+'/raw',params={'path':'Ref/proof.txt'});response.raise_for_status();assert response.content==(proof+'\n').encode()
   result['checks']=['deployed_capabilities_Node_and_three_CLI','real_Codex_Responses_DeepSeek_file_command_and_answer','PG_conversation_persistence_and_S3_input_unchanged','native_turn_cleanup_verified']
   result['passed']=True
  except Exception as e:result['failure']={'type':type(e).__name__,'message':str(e).replace(cfg['key'],'[redacted]')[:1200]}
  finally:
   if session is not None:
    try:
     closed=call('DELETE','/codex/sessions/'+session['id']);save('closed-session.json',closed)
     assert closed['phase']=='closed' and closed['cleanup_verified'];result['checks'].append('session_closed_and_cleanup_verified')
    except Exception as e:result['passed']=False;result['close_failure']={'type':type(e).__name__,'message':str(e).replace(cfg['key'],'[redacted]')[:500]}
   save('result.json',result)
 print(json.dumps(result,ensure_ascii=False));assert result['passed']

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);a=p.parse_args();review=json.loads(a.review.read_text('utf8'));assert review['classification']=='real_llm'
 for n,h in review['sources'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h,n
 run(asyncio.run(source(26)))
