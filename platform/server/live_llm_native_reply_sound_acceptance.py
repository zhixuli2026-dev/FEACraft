"""FF-F10 Windows and WSL Codex reply audio, through official live DeepSeek Responses."""

import argparse
import asyncio
import base64
from contextlib import closing, contextmanager, nullcontext
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx

from live_llm_acceptance import source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from live_llm_reply_sound_acceptance import INSTALL_AUDIO, WORKLET, analyze_audio, write_json
from testsupport import IsolatedServer


CASE = 'F10 Windows and WSL native reply sound'
MARKERS = {'windows': 'F10_WINDOWS_NATIVE_SOUND', 'wsl': 'F10_WSL_NATIVE_SOUND'}
WSL = ['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec', '/usr/bin/python3', '-I', '-S', '-u']
TURN_POST = re.compile(r'/api/v1/codex/sessions/[^/]+/turns$')
FIXTURE = '''
import json, os
from pathlib import Path
import httpx
from cryptography.fernet import Fernet
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from app.main import app
from app.services import codex_sessions, llm
from app.services.process_identity_native import observe

root = Path(__file__).parent
isolated_vault=Fernet(Fernet.generate_key())
llm._vault=lambda: isolated_vault
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())),encoding='utf8')

original_send=httpx.AsyncClient.send
async def sending(self, request, *args, **kwargs):
    if request.url.host=='api.deepseek.com':
        raise RuntimeError('FF-F10 uses the single budgeted native HTTPS transport')
    return await original_send(self,request,*args,**kwargs)
httpx.AsyncClient.send=sending

if read_only_mode or no_turn_mode:
    @app.middleware('http')
    async def deny_native_post(request, call_next):
        path=request.url.path
        if request.method=='POST' and path.startswith('/api/v1/codex/') and (read_only_mode or
                path.endswith(('/turns','/turns:stream'))):
            return Response(status_code=405)
        return await call_next(request)

def restrained(argv):
    return [argv[0], '--disable', 'multi_agent', '--disable', 'multi_agent_v2',
            '-c', 'web_search="disabled"', *argv[1:]]
windows_command=codex_sessions.command
wsl_command=codex_sessions.native_command
codex_sessions.command=lambda credential=None: restrained(windows_command(credential))
def native_command(credential=None, *, distro=None):
    program, argv=wsl_command(credential,distro=distro)
    return program, restrained(argv)
codex_sessions.native_command=native_command

@app.get('/api/v1/fixture/reply-sound-worklet.js')
async def worklet():
    return Response(audio_worklet, media_type='application/javascript')

app.mount('/',StaticFiles(directory=dist,html=True))
'''

# The WSL side binds only its own loopback. Its stdin/stdout are inherited by
# this Windows fixture. Only the isolated relay token is provided to the CLI.
LINUX_RELAY = r'''
import base64, json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Queue
token = json.loads(input())['token']
returns = Queue()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if self.path!='/responses' or self.headers.get('Authorization')!='Bearer '+token or not 0<size<2**21:
                self.send_error(403); return
            packet = base64.b64encode(self.rfile.read(size)).decode('ascii')
            print(json.dumps({'body':packet}),flush=True)
            answer = returns.get(timeout=180)
            data = base64.b64decode(answer['body'])
            self.send_response(answer['status'])
            self.send_header('Content-Type',answer['content_type'])
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception: self.send_error(502)
server = HTTPServer(('127.0.0.1',0), Handler)
def input_reader():
    for line in sys.stdin: returns.put(json.loads(line))
    server.shutdown()
threading.Thread(target=input_reader,daemon=True).start()
print(json.dumps({'port':server.server_port,'uid':os.getuid(),
                  'private_root':tempfile.mkdtemp(prefix='vibeflow-f10-',dir='/tmp')}),flush=True)
server.serve_forever()
server.server_close()
'''


class ResponsesBudget:
    def __init__(self, configuration, root, *, per_runtime=None):
        self.config, self.root = configuration, root
        self.per_runtime = per_runtime or {runtime: 1 for runtime in MARKERS}
        self.requests, self.receipts = [], []
        self.lock = threading.Lock()

    def forward(self, runtime, body):
        if not self.config:
            return 405,'text/plain',b'Inference disabled in this preflight'
        try:
            request = json.loads(body)
            assert request['model'] == self.config['model'] and request.get('stream') is True
            with self.lock:
                assert self.requests.count(runtime) < self.per_runtime.get(runtime, 0)
                self.requests.append(runtime)
            from app.services.llm import use_env_proxy
            endpoint = 'https://api.deepseek.com/responses'

            async def request_upstream():
                async with httpx.AsyncClient(timeout=110, trust_env=use_env_proxy(endpoint), follow_redirects=False) as client:
                    return await client.post(endpoint,content=body,
                        headers={'Authorization':'Bearer '+self.config['key'],'Content-Type':'application/json'})

            response = asyncio.run(request_upstream())
            assert self.config['key'].encode() not in response.content
            entry = {'runtime':runtime,'host':'api.deepseek.com','path':'/responses','status':response.status_code,
                     'model':request['model'],'response_sha256':hashlib.sha256(response.content).hexdigest()}
            for line in response.text.splitlines():
                if not line.startswith('data:') or line[5:].strip()=='[DONE]':
                    continue
                event = json.loads(line[5:])
                if event.get('type')=='response.completed':
                    terminal = event['response']
                    entry.update(response_id=terminal['id'],usage=terminal.get('usage'))
            with self.lock:
                self.receipts.append(entry)
                write_json(self.root/'official-responses.json', self.receipts)
            # Hold delivery of an actual response while Edge detaches the panel.
            released = self.root/('release-'+runtime)
            deadline=time.monotonic()+55
            while not released.is_file():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Browser did not detach within its observation window')
                time.sleep(.04)
            return response.status_code,response.headers['Content-Type'],response.content
        except Exception as error:
            with self.lock:
                self.receipts.append({'runtime':runtime,'forward_failure':type(error).__name__,
                                      'cause':type(error.__cause__).__name__ if error.__cause__ else None})
                write_json(self.root/'official-responses.json',self.receipts)
            return 502,'text/plain',b'Actual provider relay failed'


@contextmanager
def windows_relay(budget, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            size=int(self.headers.get('Content-Length','0'))
            if self.path!='/responses' or self.headers.get('Authorization')!='Bearer '+token or not 0<size<2**21:
                self.send_error(403)
                return
            status, media, content=budget.forward('windows',self.rfile.read(size))
            self.send_response(status)
            self.send_header('Content-Type',media)
            self.send_header('Content-Length',str(len(content)))
            try:
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # The official receipt is recorded before a stopped CLI disconnects.
                pass

    with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
        host=threading.Thread(target=server.serve_forever,daemon=True)
        host.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            host.join(timeout=8)


@contextmanager
def linux_relay(budget, token):
    with (budget.root/'wsl-relay.log').open('w',encoding='utf8') as err:
        process=subprocess.Popen([*WSL,'-c',LINUX_RELAY],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,
                                 text=True,encoding='utf8',creationflags=subprocess.CREATE_NO_WINDOW)
        ready=Queue()

        def serve():
            with process.stdout:
                first=process.stdout.readline()
                ready.put(json.loads(first) if first else None)
                for line in process.stdout:
                    packet=json.loads(line)
                    status,media,body=budget.forward('wsl',base64.b64decode(packet['body']))
                    try:
                        process.stdin.write(json.dumps({'status':status,'content_type':media,
                            'body':base64.b64encode(body).decode('ascii')})+'\n')
                        process.stdin.flush()
                    except (BrokenPipeError,ValueError):
                        break

        process.stdin.write(json.dumps({'token':token})+'\n')
        process.stdin.flush()
        transport=threading.Thread(target=serve,daemon=True)
        transport.start()
        try:
            value=ready.get(timeout=12)
            assert value and value['uid']==65534 and value['private_root'].startswith('/tmp/vibeflow-f10-')
            yield value
        finally:
            process.stdin.close()
            assert process.wait(timeout=15)==0
            transport.join(timeout=8)
            assert not transport.is_alive()
            if 'value' in locals() and value:
                checked=subprocess.run([*WSL,'-c','import socket,sys; s=socket.socket(); s.settimeout(1); '
                    'assert s.connect_ex(("127.0.0.1",int(sys.argv[1])))!=0',str(value['port'])],
                    capture_output=True, timeout=8,creationflags=subprocess.CREATE_NO_WINDOW)
                assert checked.returncode==0
                write_json(budget.root/'wsl-relay-cleanup.json',{'linux_uid':value['uid'],'port_closed':True})


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root,base=Path(settings['root']),settings['base']
    ids=settings['conversations']
    phase=settings['phase']
    result={'passed':False,'turns':{},'actual_native_posts':0}
    with sync_playwright() as playwright:
        edge=playwright.chromium.launch(channel='msedge')
        result['edge']=edge.version
        context=edge.new_context(viewport={'width':1530,'height':980},locale='zh-CN')
        context.add_init_script('if(location.origin==='+json.dumps(base)+')localStorage.setItem("vf_token",'+
                                json.dumps(settings['token'])+');')
        context.add_init_script(INSTALL_AUDIO)
        page=context.new_page()
        page.set_default_timeout(45000)
        capability={'sent':0,'finished':0,'responses':[]}

        def is_capability(request):
            return urlsplit(request.url).path=='/api/v1/runtime/capabilities'

        def count_capability(request, key):
            if is_capability(request):
                capability[key]+=1

        page.on('request', lambda request: count_capability(request,'sent'))
        page.on('requestfinished', lambda request: count_capability(request,'finished'))
        page.on('requestfailed', lambda request: count_capability(request,'finished'))
        page.on('response', lambda response: capability['responses'].append(response.status)
                if is_capability(response.request) else None)

        def wait_for_capabilities():
            deadline=time.monotonic()+120
            idle_since=None
            while time.monotonic()<deadline:
                page.wait_for_timeout(200)
                any_active=False
                if capability['sent'] and capability['sent']==capability['finished']:
                    with closing(sqlite3.connect((root/'test.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
                        any_active=db.execute("SELECT 1 FROM native_requests WHERE phase='active' "
                                              "AND kind IN ('runtime.wsl_probe','runtime.python_probe')").fetchone()
                    if not any_active and capability['responses'] and all(s==200 for s in capability['responses']):
                        idle_since=time.monotonic() if idle_since is None else idle_since
                        if time.monotonic()-idle_since>=1:
                            return {'sent':capability['sent'],'finished':capability['finished'],
                                    'responses':list(capability['responses'])}
                        continue
                idle_since=None
            raise AssertionError(('Workbench capabilities have not settled',capability))

        def enter(case):
            row=page.locator('[data-case-name='+json.dumps(CASE)+']')
            expect(row).to_be_visible()
            if row.get_attribute('aria-pressed')!='true':
                row.click()
            choose=page.get_by_test_id('agent-conversation')
            if not choose.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            choose.select_option(str(ids[case]))
            return choose

        def sound_switch():
            details=page.locator('details.agent-settings')
            if details.get_attribute('open') is None:
                details.locator('summary').click()
            return details.get_by_role('checkbox',name='回复提示音',exact=True)

        def assistant_reply(marker):
            return page.locator('.native-message:not(.native-user)').get_by_test_id('chat-markdown').filter(
                has_text=re.compile(r'^\s*'+re.escape(marker)+r'\s*$'))

        def listen(runtime):
            choose=enter(runtime)
            backend=page.get_by_test_id('agent-backend')
            backend.select_option(runtime)
            expect(sound_switch()).to_be_checked()
            page.locator('details.agent-settings summary').click()
            draft=page.get_by_test_id('native-input')
            expect(draft).to_be_enabled(timeout=90000)
            if runtime=='wsl':
                result['wsl_capabilities_before_launch']=wait_for_capabilities()
            page.evaluate('() => { window.f10Audio.blocks=[];window.f10Audio.connections=0; }')
            draft.fill('Reply only with the string '+MARKERS[runtime]+'. Do not use tools.')
            with page.expect_response(lambda r: r.request.method=='POST' and TURN_POST.fullmatch(urlsplit(r.url).path),
                                      timeout=135000) as submitted:
                draft.press('Enter')
            response=submitted.value
            assert response.ok, (response.status,response.text()[:250])
            result['actual_native_posts']+=1
            turn=response.json()
            choose.select_option(str(ids['other']))
            expect(choose).to_have_value(str(ids['other']))
            assert page.evaluate('window.f10Audio.blocks.length')==0
            (root/('release-'+runtime)).touch()
            page.wait_for_function('window.f10Audio.blocks.length > 10',timeout=195000)
            page.wait_for_timeout(520)
            measured=page.evaluate("""() => ({blocks:window.f10Audio.blocks,
                sample_rate:window.f10Audio.context.sampleRate,connections:window.f10Audio.connections})""")
            write_json(root/('audio-'+runtime+'.json'),measured)
            result['turns'][runtime]={'id':turn['id'],'background_conversation':True,
                                      'audio':analyze_audio(measured)}
            enter(runtime)
            expect(assistant_reply(MARKERS[runtime])).to_be_visible()
            page.wait_for_timeout(420)
            assert page.evaluate('window.f10Audio.blocks.length')==len(measured['blocks'])

        try:
            page.goto(base,wait_until='domcontentloaded')
            enter('windows')
            page.get_by_test_id('agent-backend').select_option('windows')
            preference=sound_switch()
            expect(preference).not_to_be_checked()
            if phase!='all':
                expect(assistant_reply(MARKERS['windows'])).to_be_visible()
                assert page.evaluate('window.f10Audio.contexts')==0
                result['retained_windows_assistant_selector']=True
            if phase=='history':
                result.update(silent_cold_history=True,passed=True)
                return
            if phase=='wsl-preflight':
                enter('wsl')
                page.get_by_test_id('agent-backend').select_option('wsl')
                expect(page.get_by_test_id('native-input')).to_be_enabled(timeout=90000)
                result['wsl_capabilities_before_launch']=wait_for_capabilities()
                page.get_by_role('button',name='检查运行环境',exact=True).click()
                deadline=time.monotonic()+100
                while time.monotonic()<deadline:
                    page.wait_for_timeout(250)
                    with closing(sqlite3.connect((root/'test.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
                        session=db.execute("SELECT id,phase,initialize_json,stop_json FROM native_cli_sessions "
                                           "WHERE runtime='wsl' ORDER BY created_at DESC LIMIT 1").fetchone()
                        assert db.execute("SELECT count(*) FROM native_cli_turns WHERE conversation_id=?",
                                          (ids['wsl'],)).fetchone()==(0,)
                    if session and session[1] in ('closed','failed'):
                        stop=json.loads(session[3])
                        assert session[1]=='closed' and session[2] and stop['verified'] and stop['scope_empty']
                        result.update(passed=True,preflight={'session':session[0],'initialized':True,'scope_empty':True})
                        return
                raise AssertionError('Non-inference WSL startup did not finish')
            preference.check()
            page.wait_for_function('window.f10Audio.tap || window.f10Audio.error')
            assert page.evaluate('window.f10Audio.error') is None
            page.wait_for_function('window.f10Audio.context.state === "running"')
            for runtime in (MARKERS if phase=='all' else ('wsl',)):
                listen(runtime)
            page.reload(wait_until='domcontentloaded')
            for runtime, marker in MARKERS.items():
                enter(runtime)
                expect(page.get_by_test_id('agent-backend')).to_have_value(runtime)
                expect(assistant_reply(marker)).to_be_visible()
                expect(sound_switch()).to_be_checked()
            page.wait_for_timeout(450)
            assert page.evaluate('window.f10Audio.contexts')==0
            result['silent_cold_history']=True
            page.screenshot(path=str(root/'native-reply-sound.png'))
            assert result['actual_native_posts']==(2 if phase=='all' else 1)
            result['passed']=True
        finally:
            for runtime in MARKERS:
                (root/('release-'+runtime)).touch()
            if not result['passed']:
                page.screenshot(path=str(root/'native-reply-sound-failure.png'))
            write_json(root/'native-reply-sound-browser.json',result)
            edge.close()


def windows_history(original, target):
    proof=json.loads((original/'windows-completion-independent.json').read_text(encoding='utf8'))
    assert proof['committed'] and proof['scope_empty'] and proof['official_windows_requests']==1
    assert proof['official_wsl_requests']==0
    wait_for_api_exit(original,59091)
    with closing(sqlite3.connect((original/'test.sqlite').as_uri()+'?mode=ro',uri=True)) as db, \
            closing(sqlite3.connect(target/'test.sqlite')) as clone:
        cid=proof['conversation_id']
        case_id,email=db.execute('SELECT t.case_id,u.email FROM native_cli_turns t JOIN users u '
            'ON u.id=t.created_by WHERE t.id=? AND t.conversation_id=?',(proof['windows_turn'],cid)).fetchone()
        answer=next(json.loads(payload) for payload, in db.execute(
            "SELECT payload FROM agent_events WHERE conversation_id=? AND type='message'",(cid,))
            if json.loads(payload).get('role')=='assistant')
        assert answer['content']==MARKERS['windows'] and answer['native_execution_id']==proof['windows_turn']
        db.backup(clone)
    shutil.copytree(original/'files',target/'files',dirs_exist_ok=True)
    return cid,case_id,email


def verify(args, configuration):
    phase=args.phase
    if phase in ('all','wsl'):
        assert urlsplit(configuration['base_url']).hostname=='api.deepseek.com'
        assert urlsplit(configuration['base_url']).scheme=='https'
    assert args.dist.resolve(strict=True).joinpath('index.html').is_file()
    exe=Path(shutil.which('codex.exe')).resolve(strict=True)
    elf=args.wsl_codex.resolve(strict=True) if phase!='history' else None
    api=IsolatedServer('live-f10-native-reply-sound',app='native_sound_fixture:app')
    budget=ResponsesBudget(configuration,api.root)
    token=secrets.token_hex(32)
    result={'passed':False,'provider':'deepseek','model':configuration['model'] if configuration else None,
            'mock_llm_calls':0,'phase':phase}
    print('LIVE_F10_NATIVE_EVIDENCE',api.root,flush=True)
    try:
        original=args.windows_from.resolve(strict=True) if phase!='all' else None
        if original:
            windows_id,case_id,email=windows_history(original,api.root)
        win=windows_relay(budget,token) if phase=='all' else nullcontext()
        lin=linux_relay(budget,token) if phase!='history' else nullcontext()
        with win as win_port, lin as linux:
            browser_temp=api.root/'edge-tmp'
            browser_temp.mkdir()
            settings={'VIBEFLOW_CODEX':str(exe)}
            if linux:
                settings.update(VIBEFLOW_CODEX_WSL_PATH=str(elf),VIBEFLOW_CODEX_WSL_STATE_ROOT=linux['private_root'])
            preface=f'import os\nos.environ.update({settings!r})\n'
            (api.root/'native_sound_fixture.py').write_text(preface+'dist='+repr(str(args.dist.resolve()))+'\n'+
                f'read_only_mode={phase=="history"}\nno_turn_mode={phase=="wsl-preflight"}\n'+
                'audio_worklet='+repr(WORKLET)+'\n'+FIXTURE,encoding='utf8')
            with api:
                if original:
                    api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
                    ids={'windows':windows_id}
                else:
                    case_id=api.create_case(CASE)['id']
                    ids={}
                runtimes=[] if phase=='history' else [('wsl',linux['port'])]
                if win_port:
                    runtimes.insert(0,('windows',win_port))
                for runtime,port in runtimes:
                    profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'F10 actual '+runtime,
                        'base_url':f'http://127.0.0.1:{port}',
                        'default_model':configuration['model'] if configuration else 'deepseek-flash','api_key':token})
                    ids[runtime]=api.call('POST','/conversations',{'case_id':case_id,'title':runtime+' F10',
                        'model':profile['default_model'],'permission_mode':'ask','provider_profile_id':profile['id']})['id']
                if runtimes:
                    ids['other']=api.call('POST','/conversations',{'case_id':case_id,'title':'历史 / 等待'})['id']
                subprocess.run([str(args.browser_python.resolve(strict=True)),str(Path(__file__).resolve()),'--browser'],
                    cwd=api.root,input=json.dumps({'root':str(api.root),'base':api.base,'token':api.token,
                                                   'conversations':ids,'phase':phase}),
                    encoding='utf8',check=True,timeout=650,
                    env=dict(os.environ,PYTHONIOENCODING='utf8',TEMP=str(browser_temp),TMP=str(browser_temp)))
                result['browser']=json.loads((api.root/'native-reply-sound-browser.json').read_text(encoding='utf8'))
                checks={}
                for runtime,_ in (runtimes if configuration else []):
                    marker=MARKERS[runtime]
                    identifier=result['browser']['turns'][runtime]['id']
                    turn=api.call('GET','/codex/turns/'+identifier)
                    session=api.call('GET','/codex/sessions/'+turn['session_id'])
                    assert turn['phase']=='completed' and turn['native_status']=='completed'
                    assert session['runtime']==runtime and session['cleanup_verified'] is True and session['stop']['scope_empty']
                    history=api.call('GET','/conversations/'+str(ids[runtime]))
                    answer=next(event['payload'] for event in history['events'] if event['type']=='message' and
                        event['payload'].get('native_execution_id')==identifier and event['payload'].get('role')=='assistant')
                    assert answer['content'].strip()==marker
                    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                                      trust_env=False,timeout=22) as client:
                        wire=client.get('/codex/turns/'+identifier+'/events').raise_for_status().text
                    events=[json.loads(line[5:]) for line in wire.splitlines() if line.startswith('data:')]
                    terminal=events[-1]
                    assert terminal['type']=='stream.done' and terminal['payload']=={
                        'status':'completed','native_status':'completed','error':None,'cleanup_verified':True}
                    write_json(api.root/('terminal-'+runtime+'.json'),terminal)
                    checks[runtime]={'turn':identifier,'session':turn['session_id'],'committed':True,'scope_empty':True}
                result['authoritative']=checks
            result['api_cleanup']=wait_for_api_exit(api.root,api.port)
        if win_port:
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(('127.0.0.1',win_port))!=0
            result['windows_relay_closed']=True
        if linux:
            result['wsl_relay_closed']=(api.root/'wsl-relay-cleanup.json').is_file()
        assert len(budget.receipts)==(len(runtimes) if configuration else 0) and all(item.get('status')==200 for item in budget.receipts)
        result['passed']=True
    except Exception as error:
        detail=str(error).replace(token,'[REDACTED]')
        result['failure']=(detail.replace(configuration['key'],'[REDACTED]') if configuration else detail)[:750]
        raise
    finally:
        result.update(official_requests=len(budget.requests),receipts=budget.receipts)
        assert (not configuration or configuration['key'] not in json.dumps(result)) and token not in json.dumps(result)
        write_json(api.root/'native-reply-sound-result.json',result)


if __name__=='__main__':
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id',type=int)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        parser.add_argument('--wsl-codex',type=Path)
        parser.add_argument('--windows-from',type=Path)
        parser.add_argument('--phase',choices=('all','wsl','history','wsl-preflight'),default='all')
        args=parser.parse_args()
        if args.phase!='all' and not args.windows_from:
            parser.error('--windows-from is required for a retained phase')
        if args.phase!='history' and not args.wsl_codex:
            parser.error('The WSL phases require --wsl-codex')
        if args.phase in ('all','wsl') and not args.source_profile_id:
            parser.error('The live phases require --source-profile-id and --wsl-codex')
        verify(args,asyncio.run(source(args.source_profile_id)) if args.phase in ('all','wsl') else None)
