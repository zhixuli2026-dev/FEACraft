"""Installed Claude Code, actual platform browser/PTY/vault and real DeepSeek."""
import argparse,asyncio,hashlib,json,os,shutil,socket,subprocess,sys,threading,time,uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
from live_llm_acceptance import source
from live_llm_shared_index_acceptance import SharedIndexServer,configure
from testsupport import SERVER
from app.services.process_identity_native import observe,probe
from app.services.process_stop import validate
from sqlite_acceptance_observer import SQL_OBSERVER

ROOT=SERVER.parents[1]
EXE=Path(os.environ['APPDATA'])/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'

# Observe task await chains only: no locals, SQL parameters, credentials, or
# behavioral substitution. This diagnoses a real SQLite close failure.
DIAGNOSTICS='''
import asyncio,gc,types
async def observe_tasks():
    while True:
        await asyncio.sleep(5)
        rows=[]
        for task in asyncio.all_tasks():
            chain=[];coro=task.get_coro()
            for _ in range(120):
                if coro is None:break
                code=getattr(coro,'cr_code',getattr(coro,'gi_code',getattr(coro,'ag_code',None)))
                frame=getattr(coro,'cr_frame',getattr(coro,'gi_frame',getattr(coro,'ag_frame',None)))
                if code is not None:chain.append(code.co_filename+':'+code.co_name+':'+str(frame.f_lineno if frame else 0))
                next_coro=getattr(coro,'cr_await',getattr(coro,'gi_yieldfrom',getattr(coro,'ag_await',None)))
                if next_coro is None and type(coro).__name__ in {'async_generator_asend','async_generator_athrow'}:
                    next_coro=next((x for x in gc.get_referents(coro) if isinstance(x,types.AsyncGeneratorType)),None)
                coro=next_coro
            rows.append(chain)
        (root/'task-observation.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
@app.on_event('startup')
async def start_task_observer():
    asyncio.create_task(observe_tasks())
'''


class Forwarder:
    """Forward exact request/response bodies to the authorized real service."""
    def __init__(self,config,root):
        self.receipts=[];self.count=0;self.lock=threading.Lock();outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                with (root/'forwarder-arrivals.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps({'path':self.path})+'\n')
                try:
                    assert self.path.split('?')[0] in {'/v1/messages','/v1/messages/count_tokens'}
                    assert self.headers.get('Authorization')=='Bearer '+config['key'] or self.headers.get('x-api-key')==config['key']
                    length=int(self.headers['Content-Length']);assert 0<length<2*1024*1024
                    body=self.rfile.read(length);request=json.loads(body)
                    assert request['model']==config['model']
                    with outer.lock:
                        outer.count+=1;assert outer.count<=24,'Real model call ceiling'
                    endpoint='https://api.deepseek.com/anthropic'
                    with httpx.Client(timeout=180) as client:
                        response=client.post(endpoint+self.path,content=body,
                            headers={'Authorization':'Bearer '+config['key'],'Content-Type':'application/json',
                                **{k:v for k,v in self.headers.items() if k.lower().startswith('anthropic-')}})
                    record={'host':'api.deepseek.com','status':response.status_code,'model':request['model'],'stream':request.get('stream',False),'path':self.path}
                    with outer.lock:
                        outer.receipts.append(record)
                        (root/'upstream.json').write_text(json.dumps(outer.receipts,indent=2),encoding='utf-8')
                    self.send_response(response.status_code)
                    self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                    try:
                        self.send_header('Content-Length',str(len(response.content)));self.end_headers();self.wfile.write(response.content)
                    except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                        with (root/'client-disconnections.jsonl').open('a',encoding='utf-8') as stream:
                            stream.write(json.dumps({'path':self.path,'upstream_status':response.status_code})+'\n')
                except Exception as error:
                    with outer.lock:outer.receipts.append({'forwarding_failure':type(error).__name__})
                    # An actual transport failure is an error, never a model answer.
                    self.send_error(502,'Real upstream forwarding failed')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join(5)
    @property
    def base(self):return 'http://127.0.0.1:'+str(self.server.server_port)


def verify(configuration):
    api=SharedIndexServer('live-claude-provider',app='live_fixture:app')
    launcher=api.root/'CLI tools & space'/'selected-claude.cmd';launcher.parent.mkdir()
    # Execute installed Claude with actual Read permission; no synthetic output.
    launcher.write_text('@echo off\n"'+str(EXE)+'" -p --output-format stream-json --verbose --include-partial-messages --tools Read --allowedTools Read --disable-slash-commands --system-prompt "Read the requested file once using Read and return its exact content. Use no other tools." "Read proof.txt with Read once. Reply with its exact content." > claude-output.jsonl 2> claude-stderr.txt\nset "VF_EXIT=%ERRORLEVEL%"\n> claude-exit.txt echo %VF_EXIT%\necho VF_CLAUDE_COMPLETED_%VF_EXIT%\n',encoding='utf-8')
    configure(api,{'VIBEFLOW_CLAUDE':str(launcher)},False)
    fixture_path=api.root/'live_fixture.py'
    fixture_path.write_text(fixture_path.read_text(encoding='utf-8')+DIAGNOSTICS+SQL_OBSERVER,encoding='utf-8')
    result={'passed':False,'mock_llm_calls':0,'checks':[],'model':configuration['model'],'claude_version':'2.1.218'}
    web=None;web_log=None
    def save():
        raw=json.dumps(result,ensure_ascii=False,indent=2);assert configuration['key'] not in raw
        (api.root/'claude-result.json').write_text(raw+'\n',encoding='utf-8')
    def passed(name):result['checks'].append(name);save();print('CLAUDE_PASS '+name,flush=True)
    print('CLAUDE_EVIDENCE '+str(api.root),flush=True)
    try:
        assert subprocess.run([str(EXE),'--version'],capture_output=True,text=True,timeout=20).stdout.strip()=='2.1.218 (Claude Code)'
        # Protocol mapping only; actual inference below uses the unmodified CLI.
        from types import SimpleNamespace
        from app.services.terminal_credentials import descriptor
        mapped=descriptor(SimpleNamespace(id=26,user_id=1,provider='deepseek',base_url=configuration['base_url'],
            default_model=configuration['model'],key_ciphertext='mapping-check'), 'claude')
        assert mapped['base_url']=='https://api.deepseek.com/anthropic' and mapped['wire_api']=='anthropic'
        with Forwarder(configuration,api.root) as upstream:
            try:
                with api:
                    api.create_case('CLI custom installation acceptance')
                    proof='CLAUDE_REAL_'+uuid.uuid4().hex;api.put('proof.txt',proof+'\n')
                    api.put('CLAUDE.md','Use Read to read the requested file, then return its exact contents.\n')
                    profiles=[api.call('POST','/provider-profiles',{'name':'Claude Code acceptance '+n,'provider':'anthropic',
                        'base_url':upstream.base,'default_model':configuration['model'],'api_key':configuration['key']}) for n in ['A','B']]
                    ids=[profiles[0]['id'],profiles[1]['id'],profiles[0]['id']];result['profile_ids']=ids
                    async def reject(token,query,status):
                        import websockets
                        async with websockets.connect(api.base.replace('http:','ws:')+'/ws/terminal?token='+token+'&'+query,proxy=None) as ws:
                            message=json.loads(await asyncio.wait_for(ws.recv(),30));assert message['type']=='error' and message['status']==status
                    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other Claude Code reader'})
                    asyncio.run(reject(other['token'],f'cli_harness=claude&provider_profile_id={ids[0]}',403))
                    asyncio.run(reject(api.token,f'cli_harness=claude&provider_profile_id={ids[0]}&shell=wsl',400))
                    incompatible=api.call('POST','/provider-profiles',{'name':'OpenAI protocol only','provider':'custom',
                        'base_url':upstream.base,'default_model':configuration['model'],'api_key':configuration['key']})
                    asyncio.run(reject(api.token,f'cli_harness=claude&provider_profile_id={incompatible["id"]}',400))
                    passed('actual_foreign_profile_and_unsupported_runtime_rejection')
                    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                    web_log=(api.root/'web.log').open('w',encoding='utf-8')
                    web=subprocess.Popen([shutil.which('node.exe'),str(ROOT/'platform/web/node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port',str(port),'--strictPort'],
                        cwd=ROOT/'platform/web',env=dict(os.environ,VITE_API_TARGET=api.base,VITE_WS_TARGET=api.base.replace('http:','ws:')),
                        stdout=web_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
                    result['web_birth']=observe(web.pid);url=f'http://127.0.0.1:{port}'
                    with httpx.Client(trust_env=False,timeout=2) as client:
                        deadline=time.monotonic()+30
                        while True:
                            assert web.poll() is None
                            try:
                                if client.get(url).status_code==200:break
                            except httpx.HTTPError:pass
                            if time.monotonic()>deadline:raise TimeoutError('Vite not ready')
                            time.sleep(.2)
                    (api.root/'browser.json').write_text(json.dumps({'url':url,'email':api.call('GET','/auth/me')['email'],'case_id':api.case_id,
                        'expected_command':'"'+str(launcher)+'"','harness':'claude','provider_label':'Claude Code API 配置',
                        'completion_marker':'VF_CLAUDE_COMPLETED_0','output_file':'claude-output.jsonl','provider_ids':ids,'model_key_in_browser':False},ensure_ascii=False),encoding='utf-8')
                    print('BROWSER_READY '+url,flush=True)
                    done=api.root/'browser-done.json';deadline=time.monotonic()+900
                    while not done.exists():
                        if time.monotonic()>deadline:raise TimeoutError('Browser not finished')
                        time.sleep(.5)
                    browser=json.loads(done.read_text(encoding='utf-8'));result['browser']=browser
                    assert browser['passed'];assert len(browser['provider_runs'])==3
                    result['terminal_records']=[]
                    for run,pid in zip(browser['provider_runs'],ids):
                        record=api.call('GET','/terminal/sessions/'+run['session_id']);result['terminal_records'].append(record)
                        assert record['credential']['harness']=='claude' and record['credential']['profile_id']==pid
                        assert record['phase']=='closed' and record['cleanup_verified']
                        validate(record['stop'],record['process']['root'])
                        assert probe(record['process']['root']['host'])['state']=='exited'
                        assert probe(record['process']['gate']['process'])['state']=='exited'
                        assert proof in run['output'] and configuration['key'] not in run['output']
                        events=[json.loads(x) for x in run['output'].splitlines() if x.strip()]
                        # Retain actual event structure for inspection, without synthesizing a reply.
                        assert any(x.get('type')=='result' and x.get('subtype')=='success' and not x.get('is_error') and proof in x.get('result','') for x in events)
                        assert any(x.get('type')=='assistant' and any(v.get('type')=='tool_use' and v.get('name')=='Read' for v in x.get('message',{}).get('content',[])) for x in events)
                        assert any(x.get('type')=='user' and proof in json.dumps(x) for x in events)
                    assert api.call('GET',api.prefix+'/raw?path=proof.txt',raw=True)==(proof+'\n').encode()
                    assert api.call('GET',api.prefix+'/raw?path=claude-exit.txt',raw=True).strip()==b'0'
                    passed('browser_A_B_A_real_Read_and_final_proof_three_verified_terminal_stops')
            finally:
                if web is not None:
                    if web.poll() is None:web.terminate()
                    web.wait(timeout=15);result['web_exit']=probe(result['web_birth']);assert result['web_exit']['state']=='exited'
                if web_log:web_log.close()
                if hasattr(api,'birth'):
                    deadline=time.monotonic()+10
                    while probe(api.birth)['state']=='match' and time.monotonic()<deadline:time.sleep(.05)
                    result['api_exit']={'birth':api.birth,'probe':probe(api.birth)};assert result['api_exit']['probe']['state']=='exited'
                result['upstream']=upstream.receipts
                result['real_http_responses']=len([x for x in upstream.receipts if 'status' in x]);save()
            assert result['real_http_responses']>=6 and all(x.get('status')==200 for x in upstream.receipts)
        for p in (api.root/'cli-state').rglob('*'):
            if p.is_file() and p.stat().st_size<64*1024*1024:assert configuration['key'].encode() not in p.read_bytes(),'Key persisted in CLI state'
        result['passed']=True;save();print('CLAUDE_OK '+str(api.root/'claude-result.json'),flush=True);return 0
    except BaseException as error:
        result.update(failure_type=type(error).__name__,failure=str(error).replace(configuration['key'],'[REDACTED]')[:1000]);save()
        print('CLAUDE_FAILED '+result['failure_type'],flush=True);return 1


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry']=='platform/server/live_llm_claude_provider_acceptance.py' and review['classification']=='real_llm'
    for p,h in review['sources'].items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    assert hashlib.sha256(EXE.read_bytes()).hexdigest()==review['claude_exe_sha256']
    sys.exit(verify(asyncio.run(source(args.source_profile_id))))
