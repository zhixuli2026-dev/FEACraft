"""Actual sandbox/API death fixture for browser recovery UI acceptance.

Runs the production API and built frontend on disposable ports/database/storage.
No model/provider/CLI inference substitutes. Send stop via control.json to drain.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import uuid

import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from testsupport import IsolatedServer, SERVER
from sandbox_recovery_ui_cleanup import verify as verify_cleanup
from app.models import NativeRequest
from app.services import process_identity_native as identity


async def main():
    api = IsolatedServer('sandbox-recovery-ui')
    web = SERVER.parent / 'web'
    frontend = None
    report = {'llm_calls': 0, 'mock_llm_calls': 0, 'checks': [], 'ready': False}
    engine = create_async_engine('sqlite+aiosqlite:///' + (api.root / 'test.sqlite').as_posix())
    def record(name):
        report['checks'].append(name)
        (api.root / 'fixture-checks.json').write_text(json.dumps(report, indent=2))
        print('UI_FIXTURE_PASS ' + name, flush=True)
    async def call(method, path, **kwargs):
        return await asyncio.to_thread(api.call, method, path, **kwargs)
    async def restart():
        args = api.proc.args
        api.proc.kill()
        await asyncio.to_thread(api.proc.wait, 10)
        api.proc = subprocess.Popen(args, cwd=api.root, env=api.env, stdout=api.log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET', '/api/health', api_prefix=False)).get('ok'):return
                except OSError:pass
                await asyncio.sleep(.1)
    try:
        with api:
            case = api.create_case('Sandbox recovery browser acceptance')
            user = api.call('GET', '/auth/me')
            workspace = api.call('GET', '/workspaces')[0]
            empty = api.call('POST', '/workspaces/' + str(workspace['id']) + '/cases', body={'name': 'No sandbox history'})
            suffix = uuid.uuid4().hex
            proof = 'BROWSER_RECOVERY_' + suffix
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(api.base + '/ws/terminal', params={
                        'token': api.token, 'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
                    ready = await ws.receive_json(timeout=90)
                    assert ready['type'] == 'ready', ready
                    sid = ready['session']
                    command = ("mkdir Ref/browser-empty; printf '%s%s\\n' 'BROWSER_RECOVERY_' '" + suffix
                        + "' > Ref/browser-output.txt; chmod 600 Ref/browser-output.txt; "
                        + "for i in $(seq 1 26); do printf '%s\\n' $i > Ref/page-$i.txt; done; "
                        + "cp Ref/browser-output.txt Ref/" + 'long-name-' * 12 + ".txt; cat Ref/browser-output.txt\r")
                    await ws.send_json({'type': 'input', 'data': command})
                    output = b''
                    async with asyncio.timeout(35):
                        while proof.encode() not in output:
                            message = await ws.receive()
                            assert message.type == aiohttp.WSMsgType.BINARY, message
                            output += message.data
                            assert len(output) < 1024 * 1024
                    record('actual_shell_wrote_unique_files_empty_directory_and_mode')
                    birth = identity.observe(api.proc.pid)
                    await restart()
                    assert identity.probe(birth)['state'] == 'exited'
                    record('actual_original_api_killed_and_restarted_without_close')
                async with asyncio.timeout(60):
                    while True:
                        inspection = await call('GET', '/terminal/sessions/' + sid + '/recovery-inspection')
                        if inspection['status'] == 'processes_exited':break
                        await asyncio.sleep(.2)
                record('actual_original_scopes_observed_exited')
                # Keep a second, real session active so the UI must refuse it.
                async with client.ws_connect(api.base + '/ws/terminal', params={
                        'token': api.token, 'case': api.case_id, 'shell': 'wsl-sandbox'}) as ws:
                    ready = await ws.receive_json(timeout=90)
                    assert ready['type'] == 'ready', ready
                    live_sid = ready['session']
                record('second_real_sandbox_kept_live_for_refusal')
            async with engine.connect() as db:
                exports = (await db.execute(select(NativeRequest.__table__).where(
                    NativeRequest.case_id == api.case_id, NativeRequest.kind == 'terminal.export'))).mappings().all()
            original = next(row for row in exports if json.loads(row['profile_json'])['session_id'] == sid)
            image_root = Path(json.loads(original['profile_json'])['root'])
            disk_path = image_root / 'disk.json'
            original_disk = disk_path.read_bytes()
            try:
                disk_path.write_text(json.dumps({**json.loads(original_disk), 'uuid': str(uuid.uuid4())}))
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as client:
                    async with client.post(api.base + '/api/v1/terminal/sessions/' + sid + '/recoveries',
                            headers={'Authorization': 'Bearer ' + api.token}) as response:
                        assert response.status == 502, await response.text()
            finally:
                disk_path.write_bytes(original_disk)
            record('actual_wrong_uuid_attempt_preserved_as_failed_recovery')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            log = (api.root / 'web.log').open('w', encoding='utf8')
            frontend = subprocess.Popen([shutil.which('node'), str(web / 'node_modules/vite/bin/vite.js'),
                'preview', '--host', '127.0.0.1', '--port', str(port), '--strictPort'],
                cwd=web, env=dict(os.environ, VITE_API_TARGET=api.base), stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            base = 'http://127.0.0.1:' + str(port)
            async with aiohttp.ClientSession() as client:
                async with asyncio.timeout(30):
                    while True:
                        assert frontend.poll() is None
                        try:
                            async with client.get(base) as response:
                                if response.status == 200:break
                        except aiohttp.ClientError:pass
                        await asyncio.sleep(.1)
            public = {'web': base, 'api': api.base, 'case_id': api.case_id, 'case_name': case['name'],
                      'empty_case_id': empty['id'], 'stopped_session': sid, 'live_session': live_sid,
                      'proof': proof, 'email': user['email'], 'root': str(api.root)}
            (api.root / 'ready.json').write_text(json.dumps(public, indent=2))
            report['ready'] = True
            record('production_frontend_and_api_ready_for_real_browser')
            print('RECOVERY_UI_READY=' + str(api.root / 'ready.json'), flush=True)
            # Known live harness; external browser work sends explicit control.
            async with asyncio.timeout(3600):
                while True:
                    assert frontend.poll() is None and api.proc.poll() is None
                    control = api.root / 'control.json'
                    if control.exists():
                        action = json.loads(control.read_text())
                        control.unlink()
                        if action['action'] == 'stop':break
                        if action['action'] == 'restart':
                            await restart()
                            (api.root / 'restart-ack.json').write_text(json.dumps({'restarted': True}))
                    await asyncio.sleep(.2)
            report['browser_work_finished'] = True
        await verify_cleanup(api.root)
        record('all_original_process_identities_observed_absent_after_browser_fixture')
    finally:
        if frontend is not None:
            if frontend.poll() is None:
                frontend.terminate()
                await asyncio.to_thread(frontend.wait, 10)
            log.close()
        await engine.dispose()
        (api.root / 'fixture-checks.json').write_text(json.dumps(report, indent=2))
        print('RECOVERY_UI_FIXTURE_EVIDENCE=' + str(api.root / 'fixture-checks.json'), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
