"""Run actual DeepSeek turns from the platform ACP API on disposable data."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from uuid import uuid4

def configure(api, primary, dist=None, codex_launcher=None):
    import os
    if not hasattr(primary, 'data_root'):
        primary.data_root = primary.root
        if os.environ.get('VIBEFLOW_TEST_LINUX_NATIVE_FS') == '1':
            primary.data_root = Path('/vf-acp-fixture') / uuid4().hex
            primary.data_root.mkdir()
    data = primary.data_root
    settings = {
        'VIBEFLOW_STORAGE_FS_ROOT': str(data/'files'),
        'VIBEFLOW_CODEX_STATE_ROOT': str(data/'cli-state'),
        'VIBEFLOW_AUDIT_DIR': str(primary.root/'audit'),
        'VIBEFLOW_JWT_SECRET': primary.secret,
        'VIBEFLOW_RATE_LIMIT_BACKEND': 'memory',
    }
    if primary.database_url is None:
        settings['VIBEFLOW_DATABASE_URL'] = 'sqlite+aiosqlite:///'+(data/'test.sqlite').as_posix()
    if codex_launcher is not None:
        settings['VIBEFLOW_CODEX'] = str(codex_launcher)
    code = (
        'import os\nfrom pathlib import Path\nos.environ.update('+repr(settings)+')\n'
        'from app.services import llm\nllm._VAULT_KEY_PATH=Path('+repr(str(primary.root/'.acp-vault'))+')\n'
        'from app.main import app\n')
    if dist is not None:
        code += ('from fastapi.staticfiles import StaticFiles\n'
                 +f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n')
    (api.root/'acp_fixture.py').write_text(code, encoding='utf8')


def result(api, turn, expected):
    endpoint = '/acp/turns/'+turn['id']
    deadline = time.monotonic() + 120
    events, cursor = [], 0
    while time.monotonic() < deadline:
        page = api.call('GET', endpoint+'/events?after='+str(cursor)+'&limit=7')
        events.extend(page['events'])
        if page['events']:
            cursor = page['events'][-1]['seq']
        if page['has_more']:
            continue
        current = page['turn']
        if current['phase'] in ('completed', 'cancelled', 'failed', 'recovery_required'):
            phases = (expected,) if isinstance(expected, str) else expected
            summaries = [e for e in events if e['type'] == 'prompt_finished']
            assert current['phase'] in phases and current['cleanup_verified'], (current, summaries)
            assert len({e['seq'] for e in events}) == len(events)
            return current, events
        time.sleep(.25)
    raise TimeoutError('ACP turn did not reach a verified terminal phase')


def verify(harness, config, database):
    from live_llm_acp_harness_acceptance import Relay
    from testsupport import IsolatedServer

    primary = IsolatedServer('live-acp-api-'+harness, app='acp_fixture:app', database_url=database)
    primary.secret = uuid4().hex
    peer = IsolatedServer('live-acp-peer-'+harness, app='acp_fixture:app', database_url=database)
    configure(primary, primary)
    configure(peer, primary)
    print('ACP platform evidence', primary.root, peer.root, flush=True)
    marker = 'VF_ACP_'+secrets.token_hex(8).upper()
    report = {'harness':harness, 'real_upstream':'api.deepseek.com', 'model':config['model'],
              'database':'postgresql' if database else 'sqlite', 'mock_llm_calls':0, 'checks':[], 'passed':False}

    def save():
        (primary.root/'acp-platform.json').write_text(json.dumps(report, indent=2), encoding='utf8')

    def body(text, *, thread=None):
        return dict(provider_profile_id=profile, client_message_id=uuid4().hex, content=text, thread_id=thread)

    def checked(api, turn, text):
        returned, events = result(api, turn, 'completed')
        reply = ''.join(e['text'] for e in events if e['type'] == 'agent_message_chunk')
        assert marker in reply and config['key'] not in reply and relay.token not in reply
        report['checks'].append(text)
        save()
        return returned

    try:
        with Relay(config, primary.root) as relay:
            try:
                with primary:
                    case = primary.create_case('ACP '+harness)
                    profile = primary.call('POST','/provider-profiles',{'provider':'custom',
                        'name':'Observed official DeepSeek','base_url':relay.base,
                        'default_model':config['model'],'api_key':relay.token})['id']
                    conv = primary.call('POST','/conversations',{'case_id':case['id'], 'title':harness,
                        'provider_profile_id':profile,'model':config['model'],'permission_mode':'ask'})['id']
                    endpoint = f'/acp/conversations/{conv}/{harness}/turns'
                    request = body('Remember this reference code: '+marker+'. Reply with that exact code. Use no tools.')
                    initial = primary.call('POST', endpoint, request, expect=202)
                    duplicate = primary.call('POST', endpoint, request, expect=202)
                    assert duplicate['id'] == initial['id'] and duplicate['thread_id'] == initial['thread_id']
                    primary.call('POST', endpoint, {**request,'content':'Changed text'}, expect=409)
                    checked(primary, initial, 'first real response and deduplicated admission')
                    thread = initial['thread_id']
                    outsider = primary.call('POST','/auth/register',{'email':uuid4().hex+'@example.com',
                        'password':'isolated-test-123','name':'Outsider'})['token']
                    foreign = {'Authorization':'Bearer '+outsider}
                    for verb, suffix in [('GET',''),('GET','/events'),('POST',':cancel')]:
                        primary.call(verb, '/acp/turns/'+initial['id']+suffix, expect=404, extra_headers=foreign)
                    report['checks'].append('foreign read and cancellation return 404')
                    save()
                with peer:
                    peer.token = primary.token
                    assert peer.call('GET', endpoint)[0]['thread_id'] == thread
                    resumed = peer.call('POST', endpoint,
                        body('Repeat the exact reference code from my first message. Use no tools.', thread=thread), expect=202)
                    checked(peer, resumed, 'same native session reopened in a different API process')
                    with primary:
                        held = primary.call('POST', endpoint,
                            body('ACP_HOLD_ACTUAL_REPLY. Say a sentence about mesh quality. Use no tools.', thread=thread), expect=202)
                        assert relay.held.wait(95), 'No real upstream reply was observed for cancellation'
                        try:
                            pending = peer.call('POST','/acp/turns/'+held['id']+':cancel')
                            assert pending['stop_requested'], pending
                            result(peer, held, 'cancelled')
                            report['checks'].append('peer stops verified execution after real HTTP 200')
                            save()
                        finally:
                            relay.release.set()
                    following = peer.call('POST', endpoint,
                        body('Repeat the original reference code again. Use no tools.', thread=thread), expect=202)
                    checked(peer, following, 'conversation resumes after peer cancellation')
                successes = [r for r in relay.records if r.get('status') == 200]
                failures = [r for r in relay.records if r.get('status') != 200]
                assert len(successes) >= 4 and all(r.get('failure') == 'ConnectError' for r in failures), relay.records
                if harness == 'kimi':
                    assert all(r['advertised_tools'] == 0 for r in successes), relay.records
                    report['checks'].append('private Kimi tool allowlist advertises zero tools')
                report['upstream_connection_failures'] = len(failures)
                report['passed'] = True
            finally:
                report['model_receipts'] = relay.records
    finally:
        save()


async def main(args):
    from live_llm_acceptance import source
    from live_llm_terminal_agent_acceptance import disposable_database

    configuration = await source(args.profile_id)
    if args.linux_image:
        mode = (args.harness+'-approvals') if args.approvals else args.harness or 'contracts'
        await asyncio.to_thread(linux_scenario, mode, configuration, args.linux_image)
        return
    if args.browser_python:
        await asyncio.to_thread(browser_scenario, configuration, args.dist, args.browser_python)
        return
    if args.contracts:
        async with disposable_database(args.postgres) as database:
            await asyncio.to_thread(contract_scenario, configuration, database)
        return
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(approval_scenario if args.approvals else verify, args.harness, configuration, database)


def approval_scenario(harness, config, database):
    from live_llm_acp_harness_acceptance import Relay
    from testsupport import IsolatedServer

    primary = IsolatedServer('live-acp-approval-'+harness, app='acp_fixture:app', database_url=database)
    peer = IsolatedServer('live-acp-approval-peer', app='acp_fixture:app', database_url=database)
    primary.secret = uuid4().hex
    configure(primary, primary)
    configure(peer, primary)
    fixture = primary.root/'acp_fixture.py'
    with fixture.open('a', encoding='utf8') as handle:
        handle.write("""
from app.services import acp_approval
protected = acp_approval.file_request
def review_shape(harness, call, workspace, native_input):
    import json
    raw = native_input.get('rawInput') if isinstance(native_input, dict) else None
    note = {'harness': harness, 'write_title': call.get('title') == 'Write',
            'content_types': [x.get('type') for x in call.get('content', []) if isinstance(x, dict)],
            'correlated': native_input is not None, 'kind': native_input.get('kind') if native_input else None,
            'input_fields': sorted(raw) if isinstance(raw, dict) else []}
    try:
        return protected(harness, call, workspace, native_input)
    except (ValueError, OSError) as error:
        note['refused'] = type(error).__name__
        raise
    finally:
        with (Path(__file__).parent/'permission-shapes.jsonl').open('a', encoding='utf8') as output:
            output.write(json.dumps(note)+'\\n')
acp_approval.file_request = review_shape
""")
    report = {'harness':harness, 'passed':False, 'model':config['model'], 'mock_llm_calls':0, 'checks':[]}
    marker = 'APPROVAL_'+secrets.token_hex(8).upper()
    with Relay(config, primary.root, limit=12) as relay:
        try:
            with primary, peer:
                case = primary.create_case('ACP approval fixture')
                peer.token = primary.token
                uid = primary.call('GET', '/auth/me')['id']
                profile = primary.call('POST','/provider-profiles',{'provider':'custom','name':'Owned DeepSeek',
                    'base_url':relay.base,'default_model':config['model'],'api_key':relay.token})['id']
                conv = primary.call('POST','/conversations',{'case_id':case['id'],'title':'Approved drafts',
                    'provider_profile_id':profile,'permission_mode':'ask'})['id']
                api = f'/acp/conversations/{conv}/{harness}/turns'
                outsider = primary.call('POST','/auth/register',{'email':uuid4().hex+'@example.com',
                    'password':'isolated-test-123','name':'Other account'})['token']
                for mode, name in [('reject','draft-one.txt'),('approve','draft-two.txt'),('cancel','draft-three.txt')]:
                    turn = primary.call('POST',api,{'provider_profile_id':profile,'allow_drafts':True,
                        'client_message_id':uuid4().hex,'content':
                        f'Use one file write tool to create {name} in your working directory with exactly {marker} as its content. '
                        'Call the file write tool in this turn to create the actual file. '
                        'An explanation alone does not complete the task. If the tool is declined do not retry.'},expect=202)
                    thread = turn['thread_id']
                    event_api = '/acp/turns/'+turn['id']
                    deadline = time.monotonic()+75
                    after = 0
                    while True:
                        page = peer.call('GET',event_api+'/events?after='+str(after))
                        request = next((e for e in page['events'] if e['type']=='permission'),None)
                        if request:
                            break
                        if page['events']:
                            after = page['events'][-1]['seq']
                        if page['has_more']:
                            continue
                        assert time.monotonic() < deadline and page['turn']['phase'] in ('admitted','running'), page['turn']
                        time.sleep(.25)
                    assert request['path'] == name and (request['preview'] is None and harness == 'kimi'
                                                       or marker in (request['preview'] or ''))
                    route = event_api+'/approvals/'+request['approval_id']
                    peer.call('POST',route,{'decision':'approve'},expect=404,
                              extra_headers={'Authorization':'Bearer '+outsider})
                    peer.call('POST',event_api+'/approvals/'+'0'*32,{'decision':'approve'},expect=404)
                    if mode == 'cancel':
                        assert peer.call('POST',event_api+':cancel')['stop_requested']
                        current, events = result(peer, turn, 'cancelled')
                        peer.call('POST',route,{'decision':'approve'},expect=409)
                    else:
                        accepted = peer.call('POST',route,{'decision':mode})
                        assert accepted == peer.call('POST',route,{'decision':mode})
                        peer.call('POST',route,{'decision':'reject' if mode=='approve' else 'approve'},expect=409)
                        current, events = result(peer, turn, ('completed','cancelled') if mode=='reject' else 'completed')
                    assert current['cleanup_verified']
                    target = primary.data_root/'cli-state'/'.private-cli'/'users'/str(uid)/turn['runtime']/'acp'/thread/'workspace'/name
                    drafted = [event for event in events if event['type']=='draft']
                    if mode == 'approve':
                        assert target.read_text(encoding='utf8').strip() == marker
                        assert len(drafted)==1 and drafted[0]['path']==name and drafted[0]['text'].strip()==marker
                    else:
                        assert not target.exists() and not drafted
                    report['checks'].append(mode+' actual native file operation')
            assert len([row for row in relay.records if row.get('status')==200])>=3
            assert all(row.get('status')==200 or row.get('failure')=='ConnectError' for row in relay.records)
            report['passed'] = True
        finally:
            report['receipts'] = relay.records
            (primary.root/'.acp-vault').unlink(missing_ok=True)
            (primary.root/'acp-approvals.json').write_text(json.dumps(report,indent=2),encoding='utf8')


def contract_scenario(config, database):
    from live_llm_acp_harness_acceptance import Relay
    from testsupport import IsolatedServer

    api = IsolatedServer('live-acp-contract', app='acp_fixture:app', database_url=database)
    api.secret = uuid4().hex
    configure(api, api)
    report = {'passed':False, 'mock_llm_calls':0, 'model':config['model'], 'checks':[]}
    original = 'Original case notes.\r\n'
    marks = {harness:'VF_CONTRACT_'+secrets.token_hex(8).upper() for harness in ('opencode', 'kimi')}
    with Relay(config, api.root) as relay:
        try:
            with api:
                case = api.create_case('Native contract with a model reference')
                endpoint = api.prefix+'/cli-contract'
                api.put('AGENTS.md', original)
                profile = {}
                for harness in marks:
                    profile[harness] = api.call('POST','/provider-profiles',{'provider':'custom',
                        'name':harness+' owned profile','base_url':relay.base,'default_model':config['model'],
                        'api_key':relay.token})['id']
                conversation = api.call('POST','/conversations',{'case_id':case['id'], 'title':'Contract profile restore',
                    'provider_profile_id':profile['opencode'],'permission_mode':'ask'})['id']
                outsider = api.call('POST','/auth/register',{'email':uuid4().hex+'@example.com',
                    'password':'isolated-test-123','name':'Outsider'})['token']
                foreign = {'Authorization':'Bearer '+outsider}
                foreign_profile = api.call('POST','/provider-profiles',{'provider':'custom',
                    'name':'Foreign profile','base_url':relay.base,'default_model':config['model'],
                    'api_key':relay.token},extra_headers=foreign)['id']
                initial = api.call('GET', endpoint)
                api.call('POST', endpoint, {'expected_revision':initial['revision'], 'harness':'opencode',
                    'provider_profile_id':foreign_profile},expect=403)
                api.call('GET', endpoint, expect=404, extra_headers=foreign)

                def apply(harness, expected):
                    return api.call('POST', endpoint, {'expected_revision':expected,'harness':harness,
                        'provider_profile_id':profile[harness], 'instructions':
                        'The case verification label is '+marks[harness]+'. Give it verbatim when asked.'})['backup_id']

                def prompt(harness, thread=None):
                    body = {'client_message_id':uuid4().hex, 'thread_id':thread,
                            'content':'Return the case verification label from your project instructions. Use no tools.'}
                    admitted = api.call('POST', f'/acp/conversations/{conversation}/{harness}/turns', body, expect=202)
                    assert admitted['provider_profile_id'] == profile[harness] and admitted['contract_applied']
                    completed, events = result(api, admitted, 'completed')
                    answer = ''.join(event['text'] for event in events if event['type'] == 'agent_message_chunk')
                    assert marks[harness] in answer, {'harness':harness, 'received_text':bool(answer)}
                    assert all(mark not in answer for target, mark in marks.items() if target != harness)
                    return completed['thread_id']

                original_backup = apply('opencode', initial['revision'])
                a = api.call('GET', endpoint)
                assert a['provider_profile_id'] == profile['opencode']
                thread_a = prompt('opencode')
                backup_a = apply('kimi', a['revision'])
                b = api.call('GET', endpoint)
                assert b['provider_profile_id'] == profile['kimi']
                thread_b = prompt('kimi')
                assert thread_a != thread_b
                api.call('POST', endpoint+'/backups/'+backup_a+':restore', {'expected_revision':a['revision']}, expect=409)
                api.call('POST', endpoint+'/backups/'+backup_a+':restore', {'expected_revision':b['revision']})
                restored = api.call('GET', endpoint)
                assert restored['selected'] == 'opencode' and restored['provider_profile_id'] == profile['opencode']
                assert prompt('opencode', thread_a) == thread_a
                assert prompt('opencode') != thread_a
                api.call('POST', endpoint+'/backups/'+original_backup+':restore', {'expected_revision':restored['revision']})
                final = api.call('GET', endpoint)
                assert final['selected'] is None and final['provider_profile_id'] is None
                assert api.call('GET', api.prefix+'/raw?path=AGENTS.md', raw=True) == original.encode()
                report['checks'] = ['foreign profile rejected', 'owned profile A to B to A', 'stale restore rejected',
                    'restored native thread and new thread read the frozen contract', 'original CRLF restored']
            successes = [r for r in relay.records if r.get('status') == 200]
            failures = [r for r in relay.records if r.get('status') != 200]
            assert len(successes) >= 4 and all(r['advertised_tools'] == 0 for r in successes)
            assert all(r.get('failure') == 'ConnectError' for r in failures)
            report['upstream_connection_failures'] = len(failures)
            report['passed'] = True
        finally:
            report['receipts'] = relay.records
            (api.root/'.acp-vault').unlink(missing_ok=True)
            (api.root/'acp-contract.json').write_text(json.dumps(report, indent=2), encoding='utf8')


def linux_scenario(harness, configuration, image):
    import os
    import tempfile

    server = Path(__file__).resolve().parent
    workspace = server.parents[1]
    evidence = Path(tempfile.mkdtemp(prefix='live-acp-linux-'+harness+'-', dir=workspace/'tmp'))
    selected = subprocess.run(['docker','image','inspect','--format','{{.Id}}',image], check=True,
                              capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', selected):
        raise ValueError('Linux ACP acceptance requires a local image digest')
    name = 'vf-acp-acceptance-'+uuid4().hex[:12]
    report = {'passed':False, 'image':selected, 'container':name}
    print('ACP Linux evidence', evidence, flush=True)
    try:
        command = ['docker','run','--rm','--pull','never','-i','--name',name,
                   '--security-opt','seccomp=unconfined','--workdir','/source/platform/server',
                   '--tmpfs','/vf-acp-fixture:rw,size=512m',
                   '--mount',f'type=bind,src={server},dst=/source/platform/server,readonly',
                   '--mount',f'type=bind,src={evidence},dst=/source/tmp',
                   '-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONIOENCODING=utf8',
                   '-e','VIBEFLOW_TEST_LINUX_NATIVE_FS=1']
        for key in ('HTTP_PROXY','HTTPS_PROXY'):
            proxy = os.environ.get(key, '')
            if proxy.startswith('http://127.0.0.1:'):
                command += ['-e',key+'='+proxy.replace('127.0.0.1','host.docker.internal',1)]
        command += [selected,'python','live_llm_acp_platform_acceptance.py','--linux-worker',harness]
        with (evidence/'container.log').open('w', encoding='utf8') as log:
            subprocess.run(command, input=json.dumps({'key':configuration['key'],'model':configuration['model']}),
                           text=True, check=True, timeout=400, stdout=log, stderr=subprocess.STDOUT)
        report['passed'] = True
    finally:
        remaining = subprocess.run(['docker','container','inspect','--format','{{.Name}}',name],
                                   capture_output=True, text=True)
        if remaining.returncode == 0 and remaining.stdout.strip() == '/'+name:
            subprocess.run(['docker','stop','--time','10',name], check=True, capture_output=True, timeout=25)
        report['isolated_container_removed'] = subprocess.run(
            ['docker','container','inspect','--format','{{.Id}}',name], capture_output=True).returncode != 0
        (evidence/'container-result.json').write_text(json.dumps(report, indent=2), encoding='utf8')


def browser_scenario(config, dist, browser_python):
    import shutil
    from live_llm_acp_harness_acceptance import Relay
    from testsupport import IsolatedServer

    dist = dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    api = IsolatedServer('live-acp-edge', app='acp_fixture:app')
    api.secret = uuid4().hex
    codex = Path(shutil.which('codex.exe')).resolve(strict=True)
    launcher = api.root/'case-codex-launch.cmd'
    launcher.write_text('@echo off\n"'+str(codex)+'" exec --skip-git-repo-check --json --color never '
        '--dangerously-bypass-approvals-and-sandbox --disable multi_agent --disable multi_agent_v2 '
        '-c web_search=disabled -c model_reasoning_effort=low '
        '"Read codex-proof.txt using a shell tool once. Reply with its exact content. Do not edit any file." '
        '> codex-proof.jsonl 2> codex-diagnostic.txt\necho VF_CODEX_CONTRACT_%errorlevel%\n', encoding='utf8')
    configure(api, api, dist, launcher)
    proof = 'VF_CODEX_CASE_'+secrets.token_hex(9).upper()
    report = {'passed':False, 'mock_llm_calls':0, 'model':config['model']}
    with Relay(config, api.root, responses=True, limit=18) as relay:
        try:
            with api:
                case = api.create_case('ACP browser acceptance')
                api.put('codex-proof.txt', proof+'\n')
                profiles = [api.call('POST','/provider-profiles',{'provider':'custom',
                    'name':name, 'base_url':relay.base, 'default_model':config['model'],
                    'api_key':relay.token})['id'] for name in ('DeepSeek OpenCode','DeepSeek Kimi')]
                codex_profile = api.call('POST','/provider-profiles',{'provider':'custom',
                    'name':'DeepSeek Codex Responses','base_url':relay.base.removesuffix('/v1'),
                    'default_model':config['model'],'api_key':relay.token})['id']
                conversation = api.call('POST','/conversations',{'case_id':case['id'], 'title':'ACP browser',
                    'provider_profile_id':profiles[0], 'permission_mode':'ask'})['id']
                subprocess.run([str(browser_python), str(Path(__file__).resolve()), '--edge-worker'],
                    input=json.dumps({'base':api.base, 'token':api.token, 'case':case['name'],
                        'case_id':case['id'], 'conversation':conversation, 'profiles':profiles,
                        'codex_profile':codex_profile, 'codex_proof':proof, 'root':str(api.root)}),
                    text=True, timeout=480, check=True)
                assert sum(r.get('path') == '/v1/chat/completions' and r.get('advertised_tools') == 0
                           for r in relay.records) >= 4
                assert sum(r.get('path') == '/responses' for r in relay.records) >= 2
                assert all(r.get('status') == 200 or r.get('failure')=='ConnectError' for r in relay.records), relay.records
                report['passed'] = True
        finally:
            report['model_receipts'] = relay.records
            (api.root/'.acp-vault').unlink(missing_ok=True)
            (api.root/'acp-edge.json').write_text(json.dumps(report, indent=2), encoding='utf8')


def browser_driver(instructions):
    from playwright.sync_api import expect, sync_playwright

    root = Path(instructions['root'])
    cid = instructions['conversation']
    proof = 'EDGE_ACP_'+secrets.token_hex(6).upper()
    report = {'checks':[]}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        context = browser.new_context(viewport={'width':1440, 'height':950})
        context.add_init_script('localStorage.setItem("vf_token", '+json.dumps(instructions['token'])+');')
        page = context.new_page()
        page.set_default_timeout(35000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        terminal = {'id':None, 'output':b''}

        def watch_socket(ws):
            if '/ws/terminal?' not in ws.url:
                return

            def received(payload):
                if isinstance(payload, bytes):
                    terminal['output'] = (terminal['output']+payload)[-65536:]
                else:
                    state = json.loads(payload)
                    if state.get('type') == 'ready':
                        terminal['id'] = state.get('session')

            ws.on('framereceived', received)

        page.on('websocket', watch_socket)
        active_backend = page.get_by_test_id('agent-backend')
        textarea = page.get_by_label('原生文本消息')
        response = page.get_by_test_id('acp-reply')
        send = page.get_by_role('button', name='发送', exact=True)
        selector = page.get_by_label('原生模型配置')

        def open_agent(*, mobile=False):
            case = page.locator('[data-case-name='+json.dumps(instructions['case'])+']').first
            if mobile and not case.is_visible():
                page.get_by_role('button', name='选择算例').click()
            case.click()
            if mobile:
                page.get_by_label('工作面板').select_option('agent')
            elif not active_backend.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            conversation = page.get_by_test_id('agent-conversation')
            expect(conversation.locator('option[value="'+str(cid)+'"]')).to_be_attached()
            conversation.select_option(str(cid))

        def submit(harness, prompt, profile_id, thread=None):
            textarea.fill(prompt)
            endpoint = f'/api/v1/acp/conversations/{cid}/{harness}/turns'
            with page.expect_response(lambda res: res.url.endswith(endpoint) and res.request.method == 'POST') as waiting:
                send.click()
            admitted = waiting.value
            assert admitted.status == 202, admitted.status
            payload = admitted.json()
            assert payload['provider_profile_id'] == profile_id
            if thread is not None:
                assert payload['thread_id'] == thread
            expect(response).to_contain_text(proof, timeout=95000)
            expect(page.get_by_role('status').get_by_text('回复完成')).to_be_visible(timeout=95000)
            return payload['thread_id']

        page.goto(instructions['base'], wait_until='domcontentloaded')
        open_agent()
        threads = {}
        for harness, profile in zip(('opencode','kimi'), instructions['profiles']):
            active_backend.select_option(harness)
            selector.select_option(str(profile))
            threads[harness] = submit(harness, 'Remember '+proof+'. Reply with it exactly. Use no tools.', profile)
            report['checks'].append(harness+' real first reply')
        page.set_viewport_size({'width':390,'height':844})
        page.reload(wait_until='domcontentloaded')
        open_agent(mobile=True)
        expect(active_backend).to_have_value('kimi')
        expect(response).to_contain_text(proof)
        for harness, profile in zip(('kimi','opencode'), reversed(instructions['profiles'])):
            active_backend.select_option(harness)
            expect(selector).to_have_value(str(profile))
            submit(harness, 'Repeat the code from my first message. Use no tools.', profile, threads[harness])
            report['checks'].append(harness+' mobile reload and native resume')
        ensure = page.locator('section.native-chat').evaluate(
            'node => ({panel:node.clientWidth, content:node.scrollWidth, screen:innerWidth})')
        assert 0 < ensure['content'] <= ensure['panel'] + 1, ensure
        page.screenshot(path=str(root/'acp-390.png'))
        page.get_by_label('工作面板').select_option('settings')
        contract = page.get_by_test_id('cli-contracts')
        contract_harness = contract.get_by_label('CLI 工具')
        contract_profile = contract.get_by_label('CLI 默认模型配置')

        def save_contract(harness, profile):
            contract_harness.select_option(harness)
            contract_profile.select_option(str(profile))
            contract.get_by_role('button', name='保存契约并备份').click()
            expect(contract.get_by_role('status')).to_contain_text('契约已保存')

        save_contract('kimi', instructions['profiles'][1])
        save_contract('opencode', instructions['profiles'][0])
        contract.get_by_role('button', name='恢复所选备份').click()
        expect(contract.get_by_role('status')).to_contain_text('已恢复所选备份')
        expect(contract_harness).to_have_value('kimi')
        expect(contract_profile).to_have_value(str(instructions['profiles'][1]))
        contract_profile.screenshot(path=str(root/'contract-profile-390.png'))
        report['checks'].append('mobile settings save, switch and restore the owned profile reference')
        save_contract('codex', instructions['codex_profile'])
        page.get_by_label('工作面板').select_option('terminal')
        terminal_panel = page.get_by_test_id('terminal-panel')
        provider = terminal_panel.get_by_label('Codex Responses API 配置', exact=True)
        expect(provider).to_have_value(str(instructions['codex_profile']), timeout=60000)
        terminal_panel.get_by_role('button', name='codex ▸', exact=True).click()
        deadline = time.monotonic() + 120
        while b'VF_CODEX_CONTRACT_0' not in terminal['output']:
            assert time.monotonic() < deadline and not re.search(rb'VF_CODEX_CONTRACT_[1-9]', terminal['output'])
            page.wait_for_timeout(150)
        assert terminal['id']
        terminal_panel.get_by_role('button', name='关闭终端 '+terminal['id'][:8], exact=True).click()
        headers = {'Authorization':'Bearer '+instructions['token']}
        while True:
            record = context.request.get(instructions['base']+'/api/v1/terminal/sessions/'+terminal['id'], headers=headers)
            assert record.ok, record.status
            state = record.json()
            if state['phase'] == 'closed' and state['cleanup_verified']:
                break
            assert time.monotonic() < deadline
            page.wait_for_timeout(200)
        assert state['credential']['profile_id'] == instructions['codex_profile']
        output = context.request.get(instructions['base']+
            f"/api/v1/cases/{instructions['case_id']}/raw?path=codex-proof.jsonl", headers=headers)
        assert output.ok, output.status
        events = [json.loads(line) for line in output.text().splitlines() if line]
        assert any(e.get('type') == 'item.completed' and e.get('item',{}).get('type') == 'agent_message'
                   and instructions['codex_proof'] in e['item']['text'] for e in events)
        report['checks'].append('Codex terminal inherits restored case-owned profile and reads the real proof file')
        page.get_by_label('工作面板').select_option('agent')
        expect(response).to_contain_text(proof)
        for harness, profile in zip(('opencode','kimi'), instructions['profiles']):
            name = 'edge-'+harness+'.txt'
            active_backend.select_option(harness)
            expect(selector).to_have_value(str(profile))
            page.get_by_role('checkbox', name='草稿审批').check()
            textarea.fill(f'Use one file write tool to create {name} in your working directory. Write only {proof}.')
            send.click()
            approval = page.get_by_role('region', name='草稿审批 '+name)
            expect(approval).to_be_visible(timeout=70000)
            if harness == 'opencode':
                approval.get_by_text('查看草稿预览').click()
                expect(approval.locator('pre')).to_contain_text(proof)
            approval.screenshot(path=str(root/f'approval-{harness}-390.png'))
            approval.get_by_role('button', name='允许写入一次').click()
            saved = page.get_by_text('已保存草稿 · '+name, exact=True)
            expect(saved).to_be_visible(timeout=85000)
            saved.click()
            expect(page.locator('details').filter(has=saved).locator('pre')).to_contain_text(proof)
            report['checks'].append(harness+' actual draft approval and file readback in Edge')
        page.set_viewport_size({'width':1440,'height':950})
        if not active_backend.is_visible():
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
        expect(page.get_by_text('已保存草稿 · edge-kimi.txt', exact=True)).to_be_visible()
        page.screenshot(path=str(root/'acp-1440.png'))
        assert not errors, errors
        report.update(passed=True, mobile_width=ensure, versions={'browser':browser.version})
        browser.close()
    (root/'browser.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    if sys.argv[1:] == ['--edge-worker']:
        browser_driver(json.load(sys.stdin))
        sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == '--linux-worker' and sys.argv[2] in (
            'opencode','kimi','contracts','opencode-approvals','kimi-approvals'):
        configuration = json.load(sys.stdin)
        if sys.argv[2] == 'contracts':
            contract_scenario(configuration, None)
        elif sys.argv[2].endswith('-approvals'):
            approval_scenario(sys.argv[2].split('-')[0], configuration, None)
        else:
            verify(sys.argv[2], configuration, None)
        sys.exit(0)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile-id', type=int, required=True)
    parser.add_argument('--harness', choices=['opencode','kimi'])
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--browser-python', type=Path)
    parser.add_argument('--dist', type=Path)
    parser.add_argument('--linux-image')
    parser.add_argument('--contracts', action='store_true')
    parser.add_argument('--approvals', action='store_true')
    args = parser.parse_args()
    if bool(args.browser_python) != bool(args.dist) or sum(map(bool, (args.browser_python,args.harness,args.contracts))) != 1:
        parser.error('Choose --harness, --contracts, or the pair --browser-python and --dist')
    if args.linux_image and (args.browser_python or args.postgres):
        parser.error('--linux-image uses its bundled isolated SQLite fixture')
    if args.approvals and not args.harness:
        parser.error('--approvals requires --harness')
    asyncio.run(main(args))
