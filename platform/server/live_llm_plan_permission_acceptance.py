"""Exercise Plan authorizations with real DeepSeek SSE and ordinary browser approvals."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid

import httpx

from live_llm_acceptance import source
from testsupport import IsolatedServer, SERVER


CASE = 'Plan permission acceptance'
FILE = 'Ref/plan-permission.txt'
TITLE = 'Actual read-only plan'
MEMORY = '0-caseDict/memory.md'

FIXTURE = r'''
import asyncio, json, os, time
from pathlib import Path
import httpx
from cryptography.fernet import Fernet
from fastapi.staticfiles import StaticFiles
from app.services import llm, process_identity_native
from app.services.tools import PLAN_BLOCKED_TOOLS, tools_for_openai

root = Path(__file__).parent
vault = Fernet(Fernet.generate_key())
llm._vault = lambda: vault
from app.main import app
app.mount('/', StaticFiles(directory=DIST, html=True))
(root/'api-birth.json').write_text(json.dumps(process_identity_native.observe(os.getpid())))

original_send = httpx.AsyncClient.send
original_lines = httpx.Response.aiter_lines
total = 0

def receipt(data):
    with (root/'upstream.jsonl').open('a', encoding='utf8') as out:
        out.write(json.dumps(data) + '\n')

async def send(self, request, *args, **kwargs):
    global total
    if request.url.host != 'api.deepseek.com' or not request.url.path.endswith('/chat/completions'):
        return await original_send(self, request, *args, **kwargs)
    total += 1
    assert total <= 12
    stage = (root/'stage').read_text(encoding='utf8')
    body = json.loads(request.content)
    tools = [item['function']['name'] for item in body.get('tools', [])]
    blocked = sorted(set(tools) & PLAN_BLOCKED_TOOLS)
    orphan = any(m.get('tool_calls') and (i+1 >= len(body['messages']) or body['messages'][i+1]['role'] != 'tool')
                 for i, m in enumerate(body['messages']))
    forced = None
    if stage in {'forbidden-write', 'forbidden-memory', 'ask-forbidden-write'}:
        assert not blocked and body['stream'] is True
        forced = 'update_memory' if stage == 'forbidden-memory' else 'propose_file_update'
        tool = next(tool for tool in tools_for_openai() if tool['function']['name'] == forced)
        # The fixture asks the official endpoint for a real forbidden tool call.
        # Preserve and record the application's original allowlist before changing this test request.
        body['tools'] = [tool]
        body['tool_choice'] = {'type': 'function', 'function': {'name': forced}}
        body['thinking'] = {'type': 'disabled'}
        headers = dict(request.headers)
        headers.pop('content-length', None)
        request = httpx.Request(request.method, request.url, json=body, headers=headers, extensions=request.extensions)
    response = await original_send(self, request, *args, **kwargs)
    receipt({'kind': 'http', 'stage': stage, 'status': response.status_code, 'host': request.url.host,
             'offered_read': 'read_text_file' in tools, 'offered_blocked': blocked, 'offered_count':len(tools),
             'forced': forced, 'orphan_assistant_tool': orphan})
    if stage == 'forbidden-write':
        assert response.status_code == 200
        (root/'held-response').touch()
        deadline = time.monotonic() + 30
        while not (root/'release-response').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Driver did not release official DeepSeek response')
            await asyncio.sleep(.04)
    return response

async def lines(self, *args, **kwargs):
    if self.request.url.host != 'api.deepseek.com':
        async for line in original_lines(self, *args, **kwargs):
            yield line
        return
    stage = (root/'stage').read_text(encoding='utf8')
    calls, metadata = {}, {}
    async for line in original_lines(self, *args, **kwargs):
        if line.startswith('data:'):
            value = line[5:].strip()
            if value == '[DONE]':
                receipt({'kind':'stream', 'stage':stage, 'tools':list(calls.values()), **metadata})
            elif value:
                event = json.loads(value)
                metadata.update({key: event[key] for key in ('id', 'model', 'usage') if event.get(key) is not None})
                for choice in event.get('choices') or []:
                    for tool in (choice.get('delta') or {}).get('tool_calls') or []:
                        name = (tool.get('function') or {}).get('name')
                        if name:
                            calls[tool['index']] = name
        yield line

httpx.AsyncClient.send = send
httpx.Response.aiter_lines = lines
'''


def wait_for(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError('Actual local process or provider did not reach the expected state')


def listener(port):
    with socket.socket() as probe:
        probe.settimeout(.25)
        return probe.connect_ex(('127.0.0.1', port)) == 0


@contextmanager
def peer(api):
    from app.services.process_identity_native import probe

    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    (api.root/'peer_fixture.py').write_text(
        'from app.main import app\nfrom app.services.process_identity_native import observe\n'
        'from pathlib import Path\nimport json, os\n'
        'Path("peer-birth.json").write_text(json.dumps(observe(os.getpid())))\n', encoding='utf8')
    with (api.root/'peer.log').open('w', encoding='utf8') as log:
        proc = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'peer_fixture:app', '--app-dir', str(SERVER),
                                 '--host', '127.0.0.1', '--port', str(port)], cwd=api.root, env=api.env, stdout=log,
                                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait_for(lambda: listener(port) or proc.poll() is not None, 80)
            assert proc.poll() is None
            birth = json.loads((api.root/'peer-birth.json').read_text())
            yield f'http://127.0.0.1:{port}'
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=12)
        wait_for(lambda: probe(birth)['state'] == 'exited' and not listener(port))
        (api.root/'peer-exited.json').write_text(json.dumps({'api_exited':True,'port':port,'pid':birth['pid']}), encoding='utf8')


def request(api, method, path, body=None, *, base=None, token=None):
    return httpx.request(method, (base or api.base)+'/api/v1'+path, json=body,
                        headers={'Authorization':'Bearer '+(token or api.token)}, trust_env=False, timeout=120)


def frames(response):
    assert response.status_code == 200, response.status_code
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]


def browser(data):
    from playwright.sync_api import expect, sync_playwright

    root, base, cid = Path(data['root']), data['base'], data['conversation']
    with sync_playwright() as tool:
        edge = tool.chromium.launch(channel=data['channel'])
        context = edge.new_context(viewport={'width':1536,'height':1000}, locale='zh-CN')
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(data['token'])});")
        page = context.new_page()
        try:
            page.goto(base)
            row = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            row.click()
            conversation = page.get_by_test_id('agent-conversation')
            if not conversation.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile('^Agent$')).click()
            expect(conversation).to_be_enabled(timeout=30000)
            if conversation.input_value() != str(cid):
                conversation.select_option(str(cid))
            expect(conversation).to_have_value(str(cid))
            if data['action'] == 'resume':
                page.get_by_text('会话设置与历史操作', exact=True).click()
                approve = page.get_by_test_id('approve-plan')
                expect(approve).to_be_enabled()
                page.screenshot(path=str(root/'plan-read-only.png'))
                with page.expect_response(lambda r: r.url == base+f'/api/v1/conversations/{cid}/permission'
                                          and r.request.method == 'PATCH') as change:
                    approve.click()
                assert change.value.status == 200 and change.value.json()['permission_mode'] == 'approve_writes'
                expect(approve).to_have_count(0)
                expect(page.get_by_test_id('profile-status')).to_contain_text('Plan 已批准')
                page.screenshot(path=str(root/'plan-resumed.png'))
            else:
                approve = page.get_by_role('button', name='批准执行', exact=True)
                expect(approve).to_be_enabled(timeout=30000)
                page.screenshot(path=str(root/'plan-confirm-write.png'))
                with page.expect_response(lambda r: r.url.endswith(':approve') and r.request.method == 'POST',
                                          timeout=90000) as write:
                    approve.click()
                events = [json.loads(line[5:]) for line in write.value.text().splitlines() if line.startswith('data:')]
                assert write.value.status == 200
                assert any(e['type']=='tool.result' and e['tool']=='propose_file_update' and e['ok'] for e in events)
                assert any(e['type']=='run.completed' for e in events)
                expect(approve).to_have_count(0)
                page.screenshot(path=str(root/'plan-write-applied.png'))
        except Exception:
            page.screenshot(path=str(root/f"{data['action']}-failure.png"))
            raise
        finally:
            context.close()
            edge.close()


def run_test(api, other_base, profile, args, result, before):
    root = api.root
    after = 'PLAN_APPROVED_' + uuid.uuid4().hex
    memory_probe = 'PLAN_FORBIDDEN_' + uuid.uuid4().hex
    case_path = api.prefix
    document = lambda path: api.call('GET', case_path+'/file?'+urlencode({'path':path}))['content']
    original_memory = document(MEMORY)
    cid = api.call('POST', '/conversations', {'case_id': api.case_id, 'title':TITLE,
        'provider_profile_id': profile['id'], 'model':profile['default_model'], 'permission_mode':'plan'})['id']
    entry = f'/conversations/{cid}'
    settings = {'permission_mode':'approve_writes'}

    def stage(name):
        (root/'stage').write_text(name, encoding='utf8')

    def turn(content):
        return frames(request(api,'POST',entry+'/messages',{'content':content,
            'provider_profile_id':profile['id'],'model':profile['default_model']}))

    def rejected(events):
        assert any(e['type']=='stream.error' and 'Plan' in e.get('message','') for e in events)
        assert not any(e['type'] in {'approval.requested','tool.result','tool.state'} for e in events)

    def state():
        return api.call('GET', entry)

    def use_browser(action):
        params = {'root':str(root),'base':api.base,'token':api.token,'channel':args.channel,
                  'conversation':cid,'action':action}
        temp = root/'browser-temp'
        temp.mkdir(exist_ok=True)
        subprocess.run([str(args.browser_python.resolve(strict=True)),'-B','-X','utf8',str(Path(__file__)),'--browser'],
            input=json.dumps(params),text=True,encoding='utf8',check=True,timeout=155,
            env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))

    propose = (f'请仅调用一次 propose_file_update，将隔离验收文件 {FILE} 的整个内容精确替换为 {after}，'
               '没有换行，原因写“计划审批验收”。收到工具执行结果后用一句话总结，不再调用工具。')
    stage('forbidden-write')
    with ThreadPoolExecutor(max_workers=1) as pool:
        in_flight = pool.submit(turn, propose)
        try:
            wait_for((root/'held-response').exists, 30)
            assert request(api,'PATCH',entry+'/permission',settings).status_code == 409
            cross = request(api,'PATCH',entry+'/permission',settings,base=other_base)
            result['peer_permission_during_actual_turn'] = cross.status_code
        finally:
            (root/'release-response').touch()
        rejected(in_flight.result(timeout=75))
    history = state()
    orphan = any(e['type']=='message' and e['payload'].get('tool_calls') for e in history['events'])
    result['forbidden_write_left_history_call'] = orphan
    assert not history['tool_calls'] and not history['pending_tools'] and document(FILE)==before
    if args.baseline:
        assert cross.status_code==200 and orphan and history['permission_mode']=='approve_writes'
        return

    assert cross.status_code==409 and not orphan and history['permission_mode']=='plan'
    stage('forbidden-memory')
    rejected(turn(f'请仅调用 update_memory，action=append，写入验收字符串 {memory_probe}。'))
    history = state()
    assert not history['tool_calls'] and not any(e['type']=='message' and e['payload'].get('tool_calls') for e in history['events'])
    assert document(MEMORY)==original_memory
    result['both_forbidden_calls_without_side_effects'] = True

    stage('read')
    read = turn(f'请只调用一次 read_text_file 读取 {FILE}，收到文件内容后原样返回，不再调工具。')
    assert any(e['type']=='tool.result' and e['tool']=='read_text_file' and e['ok'] and before in e['result'] for e in read)
    assert any(e['type']=='run.completed' for e in read) and document(FILE)==before
    result['real_read_after_denial'] = True

    email = 'plan-outsider@example.com'
    outsider = api.call('POST','/auth/register',{'email':email,'name':'Outside Plan','password':'isolated-plan-reader-123'})['token']
    assert request(api,'PATCH',entry+'/permission',settings,token=outsider).status_code == 404
    workspace = api.call('GET', case_path)['workspace_id']
    org = api.call('GET','/org/mine')['org']['id']
    api.call('POST',f'/org/{org}/members',{'email':email,'role':'viewer'},expect=201)
    api.call('POST',f'/org/{org}/invitation:accept',{}, extra_headers={'Authorization':'Bearer '+outsider})
    api.call('PUT',f'/workspaces/{workspace}/sharing',{'org_id':org,'role':'viewer'})
    api.call('GET',case_path,extra_headers={'Authorization':'Bearer '+outsider})
    assert request(api,'PATCH',entry+'/permission',settings,token=outsider).status_code == 404
    result['outside_and_viewer_rejected'] = True

    use_browser('resume')
    assert state()['permission_mode']=='approve_writes' and document(FILE)==before
    stage('propose')
    offered = turn(propose)
    candidates = [e['tool_call'] for e in offered if e['type']=='approval.requested']
    assert len(candidates)==1 and candidates[0]['tool']=='propose_file_update'
    pending = candidates[0]
    assert pending['args']['path']==FILE and pending['args']['content']==after
    assert state()['pending_tools'][0]['id']==pending['id'] and document(FILE)==before
    assert request(api,'PATCH',entry+'/permission',{'permission_mode':'full_access'},base=other_base).status_code==409
    result['pending_approval_unchanged_and_mode_locked'] = True
    stage('approve')
    use_browser('write')
    final = state()
    assert not final['pending_tools'] and document(FILE)==after and document(MEMORY)==original_memory
    assert final['permission_mode']=='approve_writes'
    result['browser_approved_exact_write'] = True


def check_ask(api, profile, result, before):
    cid=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual API Ask permissions',
        'provider_profile_id':profile['id'],'model':profile['default_model'],'permission_mode':'ask'})['id']
    route=f'/conversations/{cid}'

    def turn(stage, content):
        (api.root/'stage').write_text(stage,encoding='utf8')
        return frames(request(api,'POST',route+'/messages',{'content':content,
            'provider_profile_id':profile['id'],'model':profile['default_model']}))

    token='ASK_TEXT_'+uuid.uuid4().hex[:12]
    normal=turn('ask-text','只回复以下字符串，不调用工具。'+token)
    assert ''.join(e.get('text','') for e in normal if e['type']=='text.delta')==token
    assert any(e['type']=='run.completed' for e in normal)
    forbidden=turn('ask-forbidden-write',f'请调用 propose_file_update 将 {FILE} 写成 {token}。')
    assert any(e['type']=='stream.error' and e.get('code')=='tool_permission_denied' for e in forbidden)
    assert not any(e['type'] in {'approval.requested','tool.result','tool.state'} for e in (*normal,*forbidden))
    state=api.call('GET',route)
    assert not state['pending_tools'] and not state['tool_calls']
    assert api.call('GET',api.prefix+'/file?'+urlencode({'path':FILE}))['content']==before
    result['ask_text_and_structured_write_denial']=True


def start_fixture(args, prefix):
    api = IsolatedServer(prefix, app='plan_permission_fixture:app')
    (api.root/'plan_permission_fixture.py').write_text(f'DIST = {str(args.dist.resolve(strict=True))!r}\n'+FIXTURE,
                                                      encoding='utf8')
    return api


def configured_profile(api, config):
    profile = api.call('POST','/provider-profiles',{'name':'Actual Plan DeepSeek','provider':'deepseek',
        'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
    assert config['key'] not in json.dumps(profile)
    return profile


def verify_legacy(args, config):
    from app.services.process_identity_native import probe

    old = args.legacy_root.resolve(strict=True)
    tmp = (SERVER.parents[1]/'tmp').resolve()
    assert old.parent == tmp and old.name.startswith('live-plan-permission-')
    baseline = json.loads((old/'baseline-readonly-observation.json').read_text(encoding='utf8'))
    assert baseline['actual_provider_tools_in_message'] == ['propose_file_update'] and baseline['tool_rows'] == 0
    assert probe(json.loads((old/'api-birth.json').read_text()))['state']=='exited'
    api = start_fixture(args, 'live-plan-permission-legacy')
    root = api.root
    print('LIVE_PLAN_PERMISSION_LEGACY_EVIDENCE', root, flush=True)
    original_db = old/'test.sqlite'
    original_sha = hashlib.sha256(original_db.read_bytes()).hexdigest()
    with closing(sqlite3.connect(original_db.as_uri()+'?mode=ro', uri=True)) as source_db:
        with closing(sqlite3.connect(root/'test.sqlite')) as snapshot:
            source_db.backup(snapshot)
        cid, case_id = source_db.execute('select id,case_id from conversations').fetchone()
        email, = source_db.execute('select email from users').fetchone()
    shutil.copytree(old/'files', root/'files')
    result = {'passed':False,'mock_llm_calls':0}
    try:
        with api:
            api.token = api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
            profile = configured_profile(api, config)
            path = f'/conversations/{cid}'
            read = api.call('GET', f'/cases/{case_id}/file?'+urlencode({'path':FILE}))['content']
            (root/'stage').write_text('legacy-read',encoding='utf8')
            response = request(api,'POST',path+'/messages',{'content':f'只调用一次 read_text_file 读取 {FILE}，'
                '收到真实文件内容后原样返回，不再调用工具。','provider_profile_id':profile['id'],'model':profile['default_model']})
            observed = frames(response)
            assert any(e['type']=='tool.result' and e['tool']=='read_text_file' and e['ok'] and read in e['result'] for e in observed)
            assert any(e['type']=='run.completed' for e in observed)
            events = api.call('GET',path)['events']
            assert any(e['type']=='message' and any(call['name']=='propose_file_update' for call in e['payload'].get('tool_calls') or [])
                       for e in events)
            assert api.call('GET',f'/cases/{case_id}/file?'+urlencode({'path':FILE}))['content']==read
            birth = json.loads((root/'api-birth.json').read_text())
        wait_for(lambda: probe(birth)['state']=='exited' and not listener(api.port))
        records = [json.loads(line) for line in (root/'upstream.jsonl').read_text(encoding='utf8').splitlines()]
        requests = [r for r in records if r['kind']=='http']
        assert len(requests)==2 and all(r['status']==200 and not r['orphan_assistant_tool'] for r in requests)
        assert hashlib.sha256(original_db.read_bytes()).hexdigest()==original_sha
        result.update(passed=True, upstream_http_count=len(requests), original_history_preserved=True, api_exited=True)
    finally:
        (root/'live-plan-permission-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('LIVE_PLAN_PERMISSION_LEGACY_PASS',root,flush=True)


def verify(args):
    from app.services.process_identity_native import probe

    config = asyncio.run(source(args.source_profile_id))
    if args.legacy_root:
        verify_legacy(args, config)
        return
    app = start_fixture(args, 'live-plan-permission')
    root = app.root
    print('LIVE_PLAN_PERMISSION_EVIDENCE',root,flush=True)
    result = {'passed':False,'baseline':args.baseline,'model':config['model'],'mock_llm_calls':0}
    try:
        with app as api:
            api.create_case(CASE)
            profile = configured_profile(api, config)
            before = 'PLAN_ORIGINAL_' + uuid.uuid4().hex
            deadline = time.monotonic() + 15
            while True:
                saved = request(api,'PUT',api.prefix+'/file',{'path':FILE,'content':before})
                if saved.status_code == 200:
                    break
                detail = saved.json().get('detail') if saved.status_code==409 else None
                assert isinstance(detail,dict) and detail.get('code')=='storage_quota_busy'
                assert time.monotonic() < deadline, detail
                time.sleep(.25)
            if args.ask_only:
                check_ask(api,profile,result,before)
            else:
                with peer(api) as other_base:
                    run_test(api,other_base,profile,args,result,before)
            birth = json.loads((root/'api-birth.json').read_text())
        wait_for(lambda: probe(birth)['state']=='exited' and not listener(app.port))
        assert not (root/'.vault_key').exists()
        result['api_exited'] = True
        records = [json.loads(line) for line in (root/'upstream.jsonl').read_text(encoding='utf8').splitlines()]
        http = [entry for entry in records if entry['kind']=='http']
        for entry in http:
            assert entry['status']==200 and entry['host']=='api.deepseek.com'
            if entry['stage'] in {'read','forbidden-write','forbidden-memory'}:
                assert entry['offered_read'] and not entry['offered_blocked']
                if not args.baseline:
                    assert not entry['orphan_assistant_tool']
        actual = [entry for entry in records if entry['kind']=='stream']
        if args.ask_only:
            assert {row['stage'] for row in http}=={'ask-text','ask-forbidden-write'}
            assert all(row['offered_count']==0 for row in http)
            assert any(row['stage']=='ask-forbidden-write' and row['tools']==['propose_file_update'] for row in actual)
        else:
            assert any(row['stage']=='forbidden-write' and row['tools']==['propose_file_update'] for row in actual)
            if not args.baseline:
                assert any(row['stage']=='forbidden-memory' and row['tools']==['update_memory'] for row in actual)
                assert any(row['stage']=='read' and row['tools']==['read_text_file'] for row in actual)
                assert {'read','propose','approve'} <= {row['stage'] for row in http}
        result.update(passed=True, upstream_http_count=len(http))
    finally:
        (root/'live-plan-permission-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print('LIVE_PLAN_PERMISSION_PASS',root,flush=True)


if __name__=='__main__':
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id',type=int,required=True)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        parser.add_argument('--channel',default='msedge')
        parser.add_argument('--baseline',action='store_true')
        parser.add_argument('--ask-only',action='store_true')
        parser.add_argument('--legacy-root',type=Path)
        verify(parser.parse_args())
