"""Container-only real CLI inference; exact-byte relay to authorized DeepSeek."""
import hashlib,json,os,secrets,subprocess,sys,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import httpx

def main():
 cfg=json.load(sys.stdin);tool=sys.argv[1];assert tool in ('codex','opencode','kimi')
 root=Path(tempfile.mkdtemp(prefix='real-cli-'));case=root/'case';case.mkdir();state=root/'state';state.mkdir()
 proof='REAL_LINUX_CLI_'+secrets.token_hex(16);original=(proof+'\n').encode();(case/'proof.txt').write_bytes(original)
 receipts=[];result={'passed':False,'tool':tool,'provider':'deepseek','model':cfg['model'],'mock_llm_calls':0,'upstream':receipts}
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_POST(self):
   try:
    assert self.path in ('/v1/chat/completions','/v1/responses')
    assert self.headers.get('Authorization')=='Bearer '+cfg['key']
    length=int(self.headers['Content-Length']);assert 0<length<2*1024*1024
    body=self.rfile.read(length);request=json.loads(body);assert request['model']==cfg['model']
    assert len(receipts)<12,'Real model call bound exceeded'
    with httpx.Client(trust_env=False,timeout=150) as client:
     response=client.post('https://api.deepseek.com'+self.path,content=body,headers={'Authorization':self.headers['Authorization'],'Content-Type':'application/json'})
    assert cfg['key'].encode() not in response.content
    record={'path':self.path,'host':'api.deepseek.com','model':request['model'],'status':response.status_code,'bytes':len(response.content)}
    receipts.append(record)
    self.send_response(response.status_code);self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
    self.send_header('Content-Length',str(len(response.content)));self.end_headers();self.wfile.write(response.content)
   except Exception as e:
    receipts.append({'transport_failure':type(e).__name__});self.send_error(502,'Actual upstream request failed')
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 base=f'http://127.0.0.1:{server.server_port}/v1'
 env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL')}
 env.update(HOME=str(state),XDG_CONFIG_HOME=str(state/'config'),XDG_DATA_HOME=str(state/'data'),XDG_CACHE_HOME=str(state/'cache'),NO_PROXY='localhost,127.0.0.1,::1',TERM='dumb')
 prompt='Read proof.txt using your file or command tool, then reply with only the exact text found in the file. Do not guess, modify files, or perform other work.'
 if tool=='codex':
  home=state/'codex';home.mkdir();env.update(CODEX_HOME=str(home),VIBEFLOW_CODEX_PROVIDER_TOKEN=cfg['key'])
  (home/'config.toml').write_text('cli_auth_credentials_store="file"\nmodel_provider="actual_deepseek"\n[model_providers.actual_deepseek]\nname="Actual DeepSeek"\nbase_url='+json.dumps(base)+'\nenv_key="VIBEFLOW_CODEX_PROVIDER_TOKEN"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\n')
  argv=['codex','exec','--json','--skip-git-repo-check','--ephemeral','--sandbox','danger-full-access','--model',cfg['model'],prompt]
 elif tool=='opencode':
  env['ACTUAL_DEEPSEEK_KEY']=cfg['key']
  config={'providers':{'actual':{'name':'Actual DeepSeek','env':['ACTUAL_DEEPSEEK_KEY'],'package':'@opencode/ai/providers/openai-compatible','settings':{'baseURL':base},'models':{cfg['model']:{'name':cfg['model']}}}},
   'agents':{'acceptance':{'mode':'primary','steps':4,'system':'Read the requested file once with read and return its exact content. Do not delegate or use other tools.',
    'permissions':[{'action':'*','resource':'*','effect':'deny'},{'action':'read','resource':'*','effect':'allow'}]}}}
  (case/'opencode.json').write_text(json.dumps(config))
  argv=['opencode','run','--standalone','--format','json','--agent','acceptance','--model','actual/'+cfg['model'],prompt]
 else:
  env.update(KIMI_CODE_HOME=str(state),KIMI_DISABLE_TELEMETRY='1',KIMI_MODEL_NAME=cfg['model'],KIMI_MODEL_API_KEY=cfg['key'],KIMI_MODEL_PROVIDER_TYPE='openai',KIMI_MODEL_BASE_URL=base,KIMI_MODEL_MAX_CONTEXT_SIZE='131072',KIMI_MODEL_CAPABILITIES='',KIMI_SHELL_PATH='/bin/bash')
  (state/'config.toml').write_text('builtin_product_skills=false\ndisable_feedback_survey=true\n[loop_control]\nmax_steps_per_turn=4\n')
  (state/'reader.md').write_text('---\nname: file-acceptance\ndescription: Read one file\ntools:\n  - Read\nsubagents: []\n---\nRead the requested file once, then return its exact content.\n')
  (state/'skills').mkdir()
  argv=['kimi','--agent-file',str(state/'reader.md'),'--skills-dir',str(state/'skills'),'--output-format','stream-json','-p',prompt]
 try:
  started=time.monotonic();p=subprocess.run(argv,cwd=case,env=env,capture_output=True,text=True,timeout=210)
  assert cfg['key'] not in p.stdout+p.stderr
  result.update(returncode=p.returncode,elapsed_ms=round((time.monotonic()-started)*1000),stdout=p.stdout,stderr=p.stderr,proof=proof)
  assert p.returncode==0 and proof in p.stdout and (case/'proof.txt').read_bytes()==original
  assert len(receipts)>=2 and all(r.get('status')==200 for r in receipts)
  events=[json.loads(s) for s in p.stdout.splitlines() if s.strip().startswith('{')]
  if tool=='codex':
   items=[e['item'] for e in events if e.get('type')=='item.completed']
   assert any(i.get('type')=='command_execution' and proof in i.get('aggregated_output','') for i in items)
   assert any(i.get('type')=='agent_message' and proof in i.get('text','') for i in items)
  elif tool=='kimi':
   calls=[t for e in events for t in e.get('tool_calls',[])];assert len(calls)==1 and calls[0]['function']['name']=='Read'
   assert any(e.get('role')=='tool' and proof in e.get('content','') for e in events)
   assert any(e.get('role')=='assistant' and proof in e.get('content','') for e in events)
  else:
   assert any(e.get('type')=='tool_use' and e.get('part',{}).get('tool')=='read' for e in events)
   assert any(e.get('type')=='text' and proof in e.get('part',{}).get('text','') for e in events)
  result['passed']=True
 except Exception as e:result['failure']={'type':type(e).__name__,'message':str(e).replace(cfg['key'],'[redacted]')[:500]}
 finally:
  server.shutdown();server.server_close();thread.join(5)
  raw=json.dumps(result,ensure_ascii=False);assert cfg['key'] not in raw;print(raw)

if __name__=='__main__':main()
