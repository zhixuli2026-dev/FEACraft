"""Real DeepSeek tool readback of an already completed native Linux Gmsh workflow."""
import argparse,asyncio,hashlib,json,time
from pathlib import Path
import httpx
from live_llm_acceptance import source
R=Path(__file__).resolve().parents[2]

def main():
 p=argparse.ArgumentParser();p.add_argument('--review',required=True);p.add_argument('--evidence',required=True);p.add_argument('--profile-id',type=int,default=26);args=p.parse_args()
 review=json.loads((R/args.review).read_text('utf8'));assert review['classification']=='real_llm'
 for name,digest in review['sources'].items():assert hashlib.sha256((R/name).read_bytes()).hexdigest()==digest,name
 prior=R/args.evidence;native_result=json.loads((prior/'result.json').read_text('utf8'));assert native_result['passed']
 output=prior/'model-readback.json';assert not output.exists()
 account=json.loads((prior/'private/account.json').read_text('utf8'));secrets=[account['password']]
 result={'passed':False,'provider':'deepseek','host':'api.deepseek.com','application_requests':0,'upstream_HTTP_count':'not instrumented','mock_llm_calls':0}
 def write(path,value):
  data=json.dumps(value,ensure_ascii=False,indent=2);assert all(s not in data for s in secrets);path.write_text(data+'\n','utf8')
 try:
  config=asyncio.run(source(args.profile_id));secrets.append(config['key']);result['requested_model']=config['model']
  with httpx.Client(base_url='http://127.0.0.1:57700/api/v1',trust_env=False,timeout=180) as c:
   def api(method,path,**kwargs):
    r=c.request(method,path,**kwargs);r.raise_for_status();return r.json()
   auth=api('POST','/auth/login',json={'email':account['email'],'password':account['password']});secrets.append(auth['token']);c.headers['Authorization']='Bearer '+auth['token']
   case=native_result['cases'][0];response=c.get(f'/cases/{case}/raw',params={'path':'B-mesh/toolchain.json'});response.raise_for_status()
   toolchain=response.json();assert toolchain['mesh_source']=='gmsh' and toolchain['distro']=='native-linux'
   result['actual_native_execution_id']=toolchain['execution_id'];result['actual_toolchain_sha256']=hashlib.sha256(response.content).hexdigest()
   profile=api('POST','/provider-profiles',json={'provider':'deepseek','name':'Actual native Gmsh readback','base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
   conversation=api('POST','/conversations',json={'case_id':case,'title':'Read actual Gmsh toolchain','provider_profile_id':profile['id'],'model':config['model'],'permission_mode':'plan'})
   result['conversation_id']=conversation['id'];result['application_requests']=1;write(output,result)
   events=[];started=time.monotonic()
   with c.stream('POST',f'/conversations/{conversation["id"]}/messages',json={
    'content':'请只调用一次 read_text_file，读取 B-mesh/toolchain.json。按文件实际内容原样回复 execution_id、mesh_source 和 executables.gmsh 的值。不要猜测或修改文件。',
    'provider_profile_id':profile['id'],'model':config['model']}) as response:
    result['platform_HTTP_status']=response.status_code;response.raise_for_status()
    for line in response.iter_lines():
     if line.startswith('data:'):events.append(json.loads(line[5:]))
     if time.monotonic()-started>180 or len(json.dumps(events))>2097152:raise TimeoutError('Actual model readback exceeded its bound')
   write(prior/'model-events.json',events)
   assert not any(e.get('type') in {'stream.error','request.failed','request.timed_out'} for e in events)
   tools=[e for e in events if e.get('type')=='tool.result'];assert len(tools)==1 and tools[0]['tool']=='read_text_file' and tools[0]['ok']
   answer=''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
   assert all(value in answer for value in [toolchain['execution_id'],'gmsh',toolchain['executables']['gmsh']])
   assert any(e.get('type')=='run.completed' for e in events)
   stored=api('GET',f'/conversations/{conversation["id"]}')
   assert any(e['type']=='tool_result' and toolchain['execution_id'] in str(e['payload']) for e in stored['events'])
   write(prior/'model-history.json',stored);result.update(passed=True,answer=answer,elapsed_seconds=round(time.monotonic()-started,3))
 except BaseException as error:
  detail=str(error)
  for s in secrets:detail=detail.replace(s,'[REDACTED]')
  result['failure']={'type':type(error).__name__,'detail':detail[:1200]}
 finally:write(output,result)
 print(json.dumps({'passed':result['passed'],'evidence':str(output),'model':result.get('requested_model'),'application_requests':result['application_requests']}),flush=True)
 return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
