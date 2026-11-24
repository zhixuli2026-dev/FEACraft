"""Exercise real sandbox and cmd PTYs, peer API and browser session history."""
import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]
ARMED = 'FFG02_OUTPUT_ARMED'
BACKLOG = 'FFG02_ABSENT_BROWSER_OUTPUT'
AFTER = 'FFG02_INPUT_AFTER_REATTACH'
ARTIFACT = 'FFG02_SANDBOX_FILE'
FIXTURE = '''
import asyncio,json,time
from pathlib import Path
from fastapi import Depends,HTTPException
from app.main import app
from app.deps import get_current_user
from app.db import SessionLocal
from app.routers.terminal import _sessions
from app.routers.workspaces import get_owned_case
from app.services import sandbox_export_publication
from app.services.case_read_capacity import admitted

@app.get('/api/v1/test/terminal-local-observation/{identifier}')
async def get_terminal_local_observation(identifier:str, marker:str='', user=Depends(get_current_user)):
    active = _sessions.get(identifier)
    if active is None:return {'present':False}
    if active.user_id != user.id:raise HTTPException(404)
    return {'present':True, 'subscribers':len(active._subscribers),
            'marker_count':''.join(active._buf).count(marker) if marker else 0}

holding=Path('fixture-owner-credit-held')
started=Path('fixture-publication-started')
finished=Path('fixture-publication-observation.json')
actual_publish=sandbox_export_publication.publish
if os.environ.get('VIBEFLOW_FIXTURE_LEGACY_PUBLICATION_ADMISSION')=='1':
    actual_committing=sandbox_export_publication.committing
    def legacy_admission(db,case,**conditions):
        conditions.pop('wait_for_quota',None)
        return actual_committing(db,case,**conditions)
    sandbox_export_publication.committing=legacy_admission

async def observe_publication(*args,**kwargs):
    if not holding.exists():return await actual_publish(*args,**kwargs)
    started.touch()
    observed={'started_ns':time.time_ns()}
    try:
        result=await actual_publish(*args,**kwargs)
        observed['result']='published'
        return result
    except HTTPException as failure:
        observed.update(status=failure.status_code,code=failure.detail.get('code') if isinstance(failure.detail,dict) else None)
        raise
    finally:
        observed['finished_ns']=time.time_ns()
        finished.write_text(json.dumps(observed),encoding='utf-8')

sandbox_export_publication.publish=observe_publication

@app.post('/api/v1/test/hold-terminal-owner-credit/{case_id}')
async def hold_terminal_owner_credit(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db)
        async with admitted(db,case):
            holding.touch()
            try:
                async with asyncio.timeout(60):
                    while not started.exists():await asyncio.sleep(.05)
                await asyncio.sleep(1.25)
            finally:
                holding.unlink(missing_ok=True)
                await db.rollback()
    return {'released_ns':time.time_ns()}

@app.get('/api/v1/test/terminal-owner-credit-state')
async def terminal_owner_credit_state(user=Depends(get_current_user)):
    return {'held':holding.exists()}
'''


async def browser_run(data):
    from playwright.async_api import async_playwright, expect

    original, peer = data['owner'], data['peer']
    out = Path(data['root'])
    owned = {'Authorization': 'Bearer ' + data['token']}
    outside = {'Authorization': 'Bearer ' + data['outsider']}
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0, 'checks': [], 'processes': []}
    checkpoints = report['checks']
    identifiers = []
    holder = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=data['channel'])
        context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
        await context.add_init_script('localStorage.setItem("vf_token",' + json.dumps(data['token']) + ')')
        owner_page, peer_page = await context.new_page(), await context.new_page()
        terminal = owner_page.get_by_test_id('terminal-panel')
        rows = terminal.locator('.xterm-rows')
        entry = terminal.locator('.xterm-helper-textarea')
        peer_history = peer_page.get_by_test_id('native-session-history')
        peer_pty_connections = []
        owner_frames = []
        peer_page.on('websocket', lambda ws: peer_pty_connections.append(True) if urlsplit(ws.url).path == '/ws/terminal' else None)
        def observe_socket(ws):
            if urlsplit(ws.url).path != '/ws/terminal':
                return
            frames = []
            owner_frames.append(frames)
            ws.on('framereceived', lambda frame: frames.append(frame))
        owner_page.on('websocket', observe_socket)
        cdp = await context.new_cdp_session(owner_page)

        async def request(method, base, path, *, headers=owned, status=200, body=None, timeout=30000):
            response = await context.request.fetch(base + '/api/v1' + path, method=method, headers=headers, data=body, timeout=timeout)
            assert response.status == status, (method, path, response.status, (await response.text())[:300])
            return await response.json()

        async def enter_terminal(page, base):
            await page.goto(base, wait_until='domcontentloaded')
            await page.locator('[data-case-name=' + json.dumps(data['case_name']) + ']').click(timeout=30000)
            tab = page.locator('.dv-tab').filter(has_text=re.compile('^终端$'))
            await tab.click()
            box = await tab.bounding_box()
            assert box
            await page.mouse.move(720, box['y'])
            await page.mouse.down()
            await page.mouse.move(720, box['y'] - 245, steps=12)
            await page.mouse.up()

        async def create(shell, button):
            await terminal.get_by_role('button', name=button, exact=True).click()
            await terminal.get_by_role('button', name='新建终端', exact=True).click()
            row = terminal.locator('.terminal-session-tab').filter(has_text=re.compile(r'^' + shell + r'\s*·'))
            await expect(row).to_contain_text('运行中', timeout=100000)
            await expect(terminal.get_by_test_id('terminal-to-agent')).to_be_enabled(timeout=45000)
            identifier = await row.get_attribute('data-session-id')
            assert identifier
            identifiers.append(identifier)
            return identifier, row

        async def input_text(value):
            await owner_page.bring_to_front()
            await entry.focus()
            await cdp.send('Input.insertText', {'text': value})
            await owner_page.keyboard.press('Enter')

        async def history_select(panel, identifier, opening=False):
            if opening:
                await panel.locator(':scope > summary').click()
            select = panel.get_by_role('combobox', name='选择会话记录')
            await expect(select.locator('option[value=' + json.dumps(identifier) + ']')).to_have_count(1, timeout=20000)
            await select.select_option(identifier)
            details = panel.locator('.native-session-history-detail')
            await expect(details).to_contain_text(identifier, timeout=20000)
            return details

        async def explicit_close(identifier, row, initial):
            await row.get_by_role('button', name=re.compile('^关闭终端')).click()
            async with asyncio.timeout(70):
                while True:
                    record = await request('GET', peer, '/terminal/sessions/' + identifier)
                    if record['ended_at']:
                        break
                    await asyncio.sleep(.2)
            (out / f'close-receipt-{initial["shell"]}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            assert record['phase'] == 'closed' and record['cleanup_verified'] and record['stop']['verified'], record
            assert record['stop']['reason'] == 'cancelled', record
            assert all(record['process'][key] == initial['process'][key] for key in ('root', 'gate'))
            report['processes'].append(record['process'])
            return record

        try:
            await enter_terminal(owner_page, original)
            sandbox, sandbox_row = await create('wsl-sandbox', '隔离沙箱')
            sandbox_record = await request('GET', original, '/terminal/sessions/' + sandbox)
            await input_text("stty -echo; printf 'FFG02_UID_%s\\n' \"$(id -u)\"")
            await expect(rows.get_by_text('FFG02_UID_65534', exact=True)).to_be_visible(timeout=25000)
            await input_text(f"printf '%s\\n' '{ARMED}'; sleep 10; printf '%s\\n' '{BACKLOG}'")
            await expect(rows.get_by_text(ARMED, exact=True)).to_be_visible(timeout=15000)
            await terminal.get_by_role('button', name='Windows cmd', exact=True).click()
            await terminal.get_by_role('button', name='新建终端', exact=True).click()
            observation = '/test/terminal-local-observation/' + sandbox + '?marker=' + quote(BACKLOG)
            async with asyncio.timeout(5):
                while True:
                    value = await request('GET', original, observation)
                    if value['subscribers'] == 0:
                        assert value['marker_count'] == 0, value
                        break
                    await asyncio.sleep(.1)
            cmd_row = terminal.locator('.terminal-session-tab').filter(has_text=re.compile(r'^cmd\s*·'))
            await expect(cmd_row).to_contain_text('运行中', timeout=60000)
            cmd = await cmd_row.get_attribute('data-session-id')
            assert cmd
            identifiers.append(cmd)
            cmd_record = await request('GET', original, '/terminal/sessions/' + cmd)
            await expect(rows).to_contain_text(re.compile(r'[A-Za-z]:\\[^>]+>'), timeout=25000)
            await input_text('echo FFG02_OTHER_TERMINAL')
            await expect(rows).to_contain_text('FFG02_OTHER_TERMINAL', timeout=20000)
            async with asyncio.timeout(20):
                while True:
                    value = await request('GET', original, observation)
                    assert value['subscribers'] == 0, value
                    if value['marker_count']:
                        assert value['marker_count'] == 1
                        break
                    await asyncio.sleep(.15)
            assert await rows.get_by_text(BACKLOG, exact=True).count() == 0
            checkpoints.append('actual sandbox output produced once while it had zero subscribers and actual cmd was selected')

            path = '/native-sessions/terminal/' + sandbox
            owner_state = await request('GET', original, path)
            peer_state = await request('GET', peer, path)
            assert owner_state['id'] == peer_state['id'] == sandbox
            assert owner_state['owner_available'] is True and owner_state['alive'] is True
            assert peer_state['owner_available'] is False and peer_state['alive'] is None and peer_state['cleanup_verified'] is None
            for origin in (original, peer):
                await request('GET', origin, path, headers=outside, status=404)
            rejected_close = await request('DELETE', peer, '/terminal/sessions/' + sandbox, status=409)
            assert rejected_close['detail']['code'] == 'active', rejected_close
            rejected_copy = await request('POST', peer, f'/cases/{data["case_id"]}:copy', status=409, body={'name': 'FFG02 active copy'})
            copy_detail = rejected_copy['detail']
            if isinstance(copy_detail, str):
                assert '终端' in copy_detail, rejected_copy
                report['peer_case_copy_reply'] = 'terminal_active'
            else:
                assert data['backend'] == 's3' and copy_detail.get('code') == 'storage_quota_busy', rejected_copy
                report['peer_case_copy_reply'] = 'storage_quota_busy'
            await enter_terminal(peer_page, peer)
            peer_detail = await history_select(peer_history, sandbox, True)
            await expect(peer_detail).to_contain_text('当前服务未连接原执行者')
            await expect(peer_detail.get_by_role('button', name='连接已有终端')).to_have_count(0)
            checkpoints.append('real peer API and browser history distinguish same session from local control and block case copy')

            await owner_page.bring_to_front()
            owner_history = owner_page.get_by_test_id('native-session-history')
            owner_detail = await history_select(owner_history, sandbox, True)
            previous_socket_count = len(owner_frames)
            await owner_detail.get_by_role('button', name='连接已有终端').click()
            await owner_history.locator(':scope > summary').click()
            async with asyncio.timeout(25):
                while True:
                    if len(owner_frames) > previous_socket_count:
                        received = owner_frames[-1]
                        ready = [json.loads(frame) for frame in received if isinstance(frame, str) and json.loads(frame).get('type') == 'ready']
                        if ready and BACKLOG.encode() in b''.join(frame for frame in received if isinstance(frame, bytes)):
                            assert ready[-1]['session'] == sandbox
                            break
                    await asyncio.sleep(.1)
            await expect(rows).to_contain_text(BACKLOG, timeout=15000)
            assert (await request('GET', original, '/terminal/sessions/' + sandbox))['process'] == sandbox_record['process']
            await input_text(f"printf '%s\\n' '{ARTIFACT}' > Ref/terminal-multisession-proof.txt; printf '%s\\n' '{AFTER}'")
            await expect(rows).to_contain_text(AFTER, timeout=25000)
            assert (await request('GET', original, observation))['marker_count'] == 1
            if data['backend'] == 's3':
                holder = asyncio.create_task(request('POST', peer, f'/test/hold-terminal-owner-credit/{data["case_id"]}', timeout=75000))
                async with asyncio.timeout(40):
                    while not (await request('GET', peer, '/test/terminal-owner-credit-state'))['held']:
                        if holder.done():await holder
                        await asyncio.sleep(.075)
            sandbox_closed = await explicit_close(sandbox, sandbox_row, sandbox_record)
            if holder is not None:
                peer_credit = await holder
                credit = json.loads((out / 'fixture-publication-observation.json').read_text(encoding='utf-8'))
                assert credit.get('result') == 'published'
                assert credit['started_ns'] + 1_000_000_000 <= peer_credit['released_ns'] <= credit['finished_ns'], (credit, peer_credit)
                credit.update(peer_credit)
                report['real_peer_credit_during_initial_publication'] = credit
            assert sandbox_closed['tail'].count(BACKLOG) == 1 and AFTER in sandbox_closed['tail']
            assert sandbox_closed['process']['export']['session_id'] == sandbox
            assert (await request('GET', peer, '/terminal/sessions/' + cmd))['phase'] == 'active'
            file = await request('GET', peer, f'/cases/{data["case_id"]}/file?' + urlencode({'path': 'Ref/terminal-multisession-proof.txt'}))
            assert file['content'] == ARTIFACT + '\n'
            checkpoints.append('browser history reattaches the exact sandbox, command executes after backlog, and actual close publishes one file')

            await peer_detail.get_by_role('button', name='刷新核查方案').click()
            await expect(peer_detail).to_contain_text('完整停止已核验', timeout=30000)
            await peer_detail.get_by_role('button', name='读取保留终端日志').click()
            await expect(peer_detail.locator('pre')).to_contain_text(BACKLOG)
            await expect(peer_detail.locator('pre')).to_contain_text(AFTER)
            assert not peer_pty_connections
            await peer_page.screenshot(path=out / 'peer-retained-history.png')
            await explicit_close(cmd, cmd_row, cmd_record)
            await request('DELETE', peer, '/terminal/sessions/' + sandbox)
            active = await request('GET', original, '/terminal/sessions')
            assert all(row['id'] not in identifiers for row in active['sessions'])
            checkpoints.append('peer browser reads durable tail without spawning a PTY; both closes witnessed and replayed idempotently')
            report.update(passed=True, browser=browser.version, sessions=identifiers, backend=data['backend'])
        except BaseException as error:
            report['error'] = type(error).__name__
            report['detail'] = str(error)[:1800]
            await owner_page.screenshot(path=out / 'owner-failure.png')
            for identifier in identifiers:
                await context.request.delete(original + '/api/v1/terminal/sessions/' + identifier, headers=owned, timeout=15000)
            raise
        finally:
            if holder is not None:
                await asyncio.gather(holder, return_exceptions=True)
            (out / 'terminal-multisession-history.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            await browser.close()


def run_case(args, database_url, store):
    import urllib.request
    from testsupport import IsolatedServer, SERVER

    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    fixture = 'import os\n' + ('os.environ.update(' + repr(dict(store.settings())) + ')\n' if store else '')
    if args.legacy_publication_admission:
        fixture += "os.environ['VIBEFLOW_FIXTURE_LEGACY_PUBLICATION_ADMISSION']='1'\n"
    fixture += FIXTURE + '\nfrom fastapi.staticfiles import StaticFiles\n'
    fixture += "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ', html=True))\n'
    api = IsolatedServer('terminal-multisession', app='multisession_fixture:app', database_url=database_url)
    (api.root / 'multisession_fixture.py').write_text(fixture, encoding='utf-8')
    with api:
        case = api.create_case('Actual terminal session lifecycle')
        outside = api.call('POST', '/auth/register', {'email': 'other-ffg02@example.com', 'password': 'isolated-other-123', 'name': 'Other'})
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        with (api.root / 'peer-api.log').open('w', encoding='utf-8') as log:
            peer = subprocess.Popen([sys.executable, '-m', 'uvicorn', api.app, '--app-dir', str(SERVER),
                                     '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
                                    cwd=api.root, env=api.env, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 40
                while True:
                    assert peer.poll() is None and time.monotonic() < deadline, 'Peer API unavailable'
                    try:
                        with opener.open(base + '/api/health', timeout=2) as response:
                            if json.load(response).get('ok'):
                                break
                    except OSError:
                        pass
                    time.sleep(.15)
                data = {'owner': api.base, 'peer': base, 'token': api.token, 'outsider': outside['token'],
                        'root': str(api.root), 'case_name': case['name'], 'case_id': case['id'],
                        'channel': args.channel, 'backend': 's3' if store else 'fs'}
                subprocess.run([str(args.browser_python), '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--browser'],
                               input=json.dumps(data), text=True, timeout=480, check=True)
            finally:
                if peer.poll() is None:
                    peer.terminate()
                    peer.wait(timeout=15)
    assert api.proc.poll() is not None and peer.poll() is not None
    path = api.root / 'terminal-multisession-history.json'
    result = json.loads(path.read_text(encoding='utf-8'))
    from app.services import process_identity_native as identity
    from app.services.owned_process import probe_native
    try:
        for process in result.pop('processes'):
            host, gate = process['root']['host'], process['gate']['process']
            assert identity.probe(host)['state'] == 'exited'
            if gate['platform'] == 'windows':
                assert identity.probe(gate)['state'] == 'exited'
            else:
                assert asyncio.run(probe_native(process['root']['native']['distribution'], gate))['state'] == 'exited'
    except BaseException:
        result.update(passed=False, independent_root_and_gate_exit=False, actual_apis_exited=2)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    result.update(independent_root_and_gate_exit=True, actual_apis_exited=2)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('TERMINAL_MULTISESSION_OK ' + str(api.root), flush=True)


async def main(args):
    from live_llm_terminal_agent_acceptance import disposable_database

    store = nullcontext()
    if args.s3:
        from s3testsupport import ObjectStore
        store = ObjectStore(executable=ROOT / 'third_party/seaweed-4.52-runtime/weed', volume_max=6)
    async with disposable_database(args.postgres) as database_url:
        with store as active:
            await asyncio.to_thread(run_case, args, database_url, active)
    if args.s3:
        assert active.proc is not None and active.proc.poll() is not None


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser_run(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        parser.add_argument('--postgres', action='store_true')
        parser.add_argument('--s3', action='store_true')
        parser.add_argument('--legacy-publication-admission', action='store_true',
                            help='Exercise the former immediate admission against a real held peer quota')
        asyncio.run(main(parser.parse_args()))
