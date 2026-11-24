"""Actual native Codex with a genuinely busy one-slot relay and live DeepSeek."""
from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

import httpx

from live_llm_acceptance import source
from live_llm_codex_terminal_provider_acceptance import EXE, Forwarder
from live_llm_codex_native_responses_acceptance import OPTIONS
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer


def until(check, label, seconds=55):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=check()
        if value:
            return value
        time.sleep(.08)
    raise TimeoutError(label)


class CapacityRelay(Forwarder):
    """Admission only. Accepted requests and replies still use Forwarder."""
    def __init__(self, config, root):
        super().__init__(config,root)
        self.configuration=config
        self.slot=threading.BoundedSemaphore(1)
        self.holder=uuid4().hex
        self.release_holder=threading.Event()
        self.holding=threading.Event()
        self.tool_return=threading.Event()
        self.wait_after_tool=False
        self.admission=[]
        outer=self
        upstream=self.server.RequestHandlerClass

        class Handler(upstream):
            def do_POST(self):
                if (self.path!='/responses' or self.headers.get('Authorization')!='Bearer '+config['key']):
                    self.send_error(403)
                    return
                length=int(self.headers['Content-Length'])
                assert 0<length<2*1024*1024
                body=self.rfile.read(length)
                payload=json.loads(body)
                items=payload.get('input',[])
                self.occupied_client=self.headers.get('X-VF-Hold')==outer.holder
                tool_result=isinstance(items,list) and any(isinstance(item,dict)
                    and item.get('type')=='function_call_output' for item in items)
                if outer.wait_after_tool and tool_result and not self.occupied_client:
                    outer.tool_return.set()
                    if not outer.holding.wait(25):
                        self.send_error(504)
                        return
                admitted=outer.slot.acquire(blocking=False)
                with outer.lock:
                    outer.admission.append({'holder':self.occupied_client,'tool_result':tool_result,
                        'admitted':admitted,'at':time.monotonic()})
                if not admitted:
                    error=json.dumps({'error':{'code':'serverOverloaded',
                        'type':'server_overloaded','message':'Selected model is at capacity'}}).encode()
                    self.send_response(503)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(error)))
                    self.end_headers()
                    self.wfile.write(error)
                    return
                original=self.rfile
                self.rfile=BytesIO(body)
                try:
                    super().do_POST()
                finally:
                    self.rfile=original
                    outer.slot.release()

            def end_headers(self):
                if getattr(self,'occupied_client',False):
                    outer.holding.set()
                    outer.release_holder.wait(45)
                super().end_headers()

        self.server.RequestHandlerClass=Handler

    def occupy(self):
        self.holding.clear()
        self.release_holder.clear()
        results=[]

        def forward():
            try:
                with httpx.Client(trust_env=False,timeout=65) as client:
                    res=client.post(self.base+'/responses',headers={
                        'Authorization':'Bearer '+self.configuration['key'],'X-VF-Hold':self.holder,
                    },json={'model':self.configuration['model'],'input':[
                        {'role':'user','content':'Return a single word: READY.'}],
                        'stream':False,'max_output_tokens':64})
                    results.append(res.status_code)
            except httpx.HTTPError as error:
                results.append(type(error).__name__)

        thread=threading.Thread(target=forward,daemon=True)
        thread.start()
        try:
            assert self.holding.wait(25),'Genuine competing model request never held the slot'
            assert self.receipts[-1].get('status')==200
        except BaseException:
            self.release_holder.set()
            thread.join(25)
            raise

        def release():
            self.release_holder.set()
            thread.join(25)
            assert not thread.is_alive() and results==[200],results
        return release

    def __exit__(self,*arguments):
        self.release_holder.set()
        return super().__exit__(*arguments)


def configure(api,dist=None):
    settings={'VIBEFLOW_CODEX':str(EXE)}
    code='import os\nos.environ.update('+repr(settings)+')\nfrom pathlib import Path\n'
    code+='from app.services import llm\nllm._VAULT_KEY_PATH=Path(__file__).with_name(".vault_key")\n'
    code+='from app.main import app\n'+OPTIONS
    if dist:
        code+='\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory='+repr(str(dist))+',html=True))\n'
    (api.root/'capacity_fixture.py').write_text(code,encoding='utf8')


TERMINAL={'completed','failed','cancelled','interrupted','recovery_required','evidence_invalid'}


def public_events(client, identifier):
    result=client.get(f'/codex/turns/{identifier}/events')
    assert result.status_code==200,result.status_code
    return [json.loads(line[5:]) for line in result.text.splitlines() if line.startswith('data:')]


EDGE = """
import json,re,sys,time
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
cfg=json.load(sys.stdin); out=Path(cfg['out'])
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    context=browser.new_context(viewport={'width':1440,'height':900})
    page=context.new_page()
    page.add_init_script('localStorage.setItem("vf_token",'+json.dumps(cfg['token'])+');')
    page.goto(cfg['address'],wait_until='domcontentloaded')
    page.locator('[data-case-name="Native capacity checks"]').click()
    page.locator('.dv-tab:visible').filter(has_text=re.compile('^Agent$')).click()
    page.get_by_test_id('agent-conversation').select_option(str(cfg['conversation']))
    page.get_by_test_id('agent-backend').select_option('windows')
    page.get_by_test_id('native-input').fill('Use no tools. Reply with READY.')
    send=page.get_by_role('button',name='发送',exact=True)
    expect(send).to_be_enabled()
    (out/'edge-ready.txt').write_text('ready',encoding='utf8')
    deadline=time.monotonic()+35
    while not (out/'edge-go.txt').exists():
        if time.monotonic()>deadline:
            raise TimeoutError('Actual occupied relay was not ready')
        time.sleep(.05)
    with page.expect_response(lambda r:r.request.method=='POST' and '/codex/sessions/' in r.url and r.url.endswith('/turns'),
                              timeout=35000) as submitted:
        send.click()
    assert submitted.value.ok,submitted.value.status
    turn=submitted.value.json()['id']
    status=page.get_by_test_id('native-capacity-backoff')
    expect(status).to_contain_text('1/5',timeout=30000)
    status.screenshot(path=out/'capacity-desktop.png',timeout=2000)
    page.set_viewport_size({'width':390,'height':844})
    page.get_by_label('工作面板',exact=True).select_option('agent')
    expect(status).to_be_visible(timeout=1500)
    widths=status.evaluate('node=>[node.clientWidth,node.scrollWidth]')
    assert widths[1]<=widths[0]+2
    status.screenshot(path=out/'capacity-mobile.png',timeout=1500)
    with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith(turn+':cancel'),timeout=35000) as cancelled:
        page.get_by_test_id('native-stop').click()
    assert cancelled.value.ok,cancelled.value.status
    expect(page.get_by_test_id('native-turn-status')).to_contain_text('已取消',timeout=35000)
    expect(status).to_have_count(0)
    (out/'capacity-browser.json').write_text(json.dumps({'passed':True,'turn':turn,'mobile_widths':widths,
        'actual_backoff_visible':True,'cancelled_from_browser':True},indent=2),encoding='utf8')
    context.close();browser.close()
"""


def browser_cancel(api, relay, conversation, executable):
    inputs={'out':str(api.root),'address':api.base,'token':api.token,'conversation':conversation}
    with (api.root/'edge.log').open('w',encoding='utf8') as log:
        browser=subprocess.Popen([str(executable),'-c',EDGE],stdin=subprocess.PIPE,
            stdout=log,stderr=subprocess.STDOUT,text=True,encoding='utf8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        hold=None
        try:
            browser.stdin.write(json.dumps(inputs))
            browser.stdin.close()
            until(lambda: (api.root/'edge-ready.txt').exists() or browser.poll() is not None,
                  'Real browser did not reach the native composer',35)
            assert browser.poll() is None,'Native browser setup failed; see edge.log'
            hold=relay.occupy()
            (api.root/'edge-go.txt').write_text('go',encoding='utf8')
            assert browser.wait(timeout=65)==0,'Native browser cancellation failed; see edge.log'
            return json.loads((api.root/'capacity-browser.json').read_text(encoding='utf8'))
        finally:
            try:
                if hold:
                    hold()
            finally:
                if browser.poll() is None:
                    browser.terminate()
                    browser.wait(timeout=10)


def verify(cfg, database, browser_python=None, dist=None):
    assert EXE.is_file()
    api=IsolatedServer('live-f13-capacity',app='capacity_fixture:app',database_url=database)
    configure(api,dist)
    print('F13_EVIDENCE',api.root,flush=True)
    report={'passed':False,'model':cfg['model'],'mock_llm_calls':0,'checks':[]}
    relay=CapacityRelay(cfg,api.root)
    try:
        with api,relay:
            case=api.create_case('Native capacity checks')
            with httpx.Client(base_url=api.base+'/api/v1',trust_env=False,
                              headers={'Authorization':'Bearer '+api.token},timeout=35) as client:
                def call(method,path,data=None):
                    res=client.request(method,path,json=data)
                    assert res.status_code==200,(method,path,res.status_code)
                    return res.json()

                profile=call('POST','/provider-profiles',{'name':'Real native capacity','provider':'custom',
                    'base_url':relay.base,'default_model':cfg['model'],'api_key':cfg['key']})['id']

                def start(content, *, permission='ask'):
                    conv=call('POST','/conversations',{'case_id':case['id'],'title':'Native capacity',
                        'provider_profile_id':profile,'model':cfg['model'],'permission_mode':permission})
                    session=call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],
                        'provider_profile_id':profile,'provider_wire_api':'responses'})
                    body={'content':content,'client_message_id':uuid4().hex,'timeout_seconds':110,
                          'model_options':{'effort':'low'}}
                    target=f"/codex/sessions/{session['id']}/turns"
                    return target,body,call('POST',target,body)['id']

                def status(identifier):
                    return call('GET','/codex/turns/'+identifier)

                def settled(identifier):
                    def check():
                        value=status(identifier)
                        return value if value['phase'] in TERMINAL else None
                    return until(check,'Native capacity turn did not settle',85)

                def refusal(since):
                    return any(not row['admitted'] for row in relay.admission[since:])

                marker='CAPACITY_REAL_'+uuid4().hex[:12].upper()
                hold=relay.occupy()
                began=time.monotonic()
                try:
                    opens=len(relay.admission)
                    target,body,identifier=start('Use no tools. Reply exactly '+marker)
                    until(lambda: refusal(opens),'Native CLI did not meet the actual occupied gateway')
                finally:
                    hold()
                recovered=settled(identifier)
                retained=public_events(client,identifier)
                backoff=next((e for e in retained if e['type']=='capacity.backoff'),None)
                assert backoff is not None,(recovered,[(e['type'],e.get('payload',{}).get('status')) for e in retained])
                assert 5<=backoff['payload']['delay_seconds']<=5.5
                assert recovered['phase']=='completed' and recovered['cleanup_verified'] is True,recovered
                assert [row['status'] for row in recovered['attempts']]==['failed','completed']
                assert recovered['attempts'][0]['message_id']==body['client_message_id']
                assert '-continue-' in recovered['attempts'][1]['message_id']
                assert call('POST',target,body)['id']==identifier
                assert marker in ''.join(e['payload']['text'] for e in retained if e['type']=='text.delta')
                report['checks'].append('real CLI failure triggers measured backoff and one safe continuing attempt')
                report['recovered']={'attempts':recovered['attempts'],'backoff':backoff['payload'],
                    'elapsed_seconds':round(time.monotonic()-began,2)}

                hold=relay.occupy()
                try:
                    opens=len(relay.admission)
                    _,_,stopping=start('Use no tools. Reply with READY.')
                    until(lambda: refusal(opens),'Waiting cancellation gate did not receive a native request')
                    time.sleep(.75)
                    call('POST',f'/codex/turns/{stopping}:cancel')
                finally:
                    hold()
                cancelled=settled(stopping)
                assert cancelled['phase']=='cancelled' and cancelled['cleanup_verified'] is True
                assert len(cancelled['attempts'])==1
                assert any(e['type']=='capacity.backoff' for e in public_events(client,stopping))
                report['checks'].append('cancellation during actual backoff closes the scope without a second attempt')

                relay.wait_after_tool=True
                relay.tool_return.clear()
                relay.holding.clear()
                _,_,effect=start(
                    "Run exactly one shell command. Use PowerShell Add-Content to append EFFECT_ONCE "
                    "to Ref/capacity-marker.txt, then answer using the tool result. Do not delegate.",
                    permission='full_access')
                assert relay.tool_return.wait(45),'Actual native command did not reach its model result continuation'
                hold=relay.occupy()
                try:
                    stopped=settled(effect)
                finally:
                    hold()
                relay.wait_after_tool=False
                assert stopped['phase']=='failed' and stopped['error']=='CodexCapacityRequiresReview',stopped
                assert len(stopped['attempts'])==1 and stopped['cleanup_verified'] is True
                acted=public_events(client,effect)
                commands=[e for e in acted if e['type']=='item.started' and e['payload'].get('type')=='commandExecution']
                assert len(commands)==1 and not any(e['type']=='capacity.backoff' for e in acted)
                assert any(e['type']=='item.completed' and e['payload'].get('type')=='commandExecution'
                           and e['payload'].get('exitCode')==0 for e in acted)
                report['checks'].append('one actual command followed by a capacity failure requires review and is never replayed')
                if browser_python:
                    conv=call('POST','/conversations',{'case_id':case['id'],'title':'Browser cancellation',
                        'provider_profile_id':profile,'model':cfg['model'],'permission_mode':'ask'})
                    proof=browser_cancel(api,relay,conv['id'],browser_python)
                    observed=settled(proof['turn'])
                    assert observed['phase']=='cancelled' and observed['cleanup_verified'] is True
                    assert len(observed['attempts'])==1
                    report['checks'].append('Edge shows durable capacity status and cancels from the phone-width view')
                report['passed']=True
    finally:
        (api.root/'.vault_key').unlink(missing_ok=True)
        report['upstream']=relay.receipts
        report['admission']=relay.admission
        report['real_http_200']=sum(row.get('status')==200 for row in relay.receipts)
        report['passed']=report['passed'] and report['real_http_200']==(6 if browser_python else 5)
        raw=json.dumps(report,indent=2)
        assert cfg['key'] not in raw
        (api.root/'capacity-result.json').write_text(raw,encoding='utf8')
    assert report['passed']


async def main(args):
    cfg=await source(args.source_profile_id)
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(verify,cfg,database,args.browser_python,args.dist.resolve(strict=True) if args.dist else None)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--browser-python',type=Path)
    parser.add_argument('--dist',type=Path)
    args=parser.parse_args()
    if bool(args.browser_python)!=bool(args.dist):
        parser.error('--browser-python and --dist are used together')
    asyncio.run(main(args))
