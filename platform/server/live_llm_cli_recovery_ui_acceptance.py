"""Actual S3/CLI interruption and recovery through the built product browser UI."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import httpx
from testsupport import IsolatedServer, SERVER
from s3testsupport import ObjectStore
from live_llm_acceptance import source, FIXTURE as LIVE
from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_cli_inference_acceptance import FIXTURE as INFERENCE
from live_llm_cli_approval_acceptance import FIXTURE as APPROVAL, TERMINAL

ROOT = SERVER.parents[1]

DIAGNOSTICS = r'''
import traceback
from app.services import storage_admission
_original_busy = storage_admission.busy
def observe_busy():
 with (_base/'quota-busy.jsonl').open('a', encoding='utf8') as stream:
  stream.write(json.dumps([{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_stack()])+'\n')
 return _original_busy()
storage_admission.busy = observe_busy
from app.services import owner_storage_budget
timed_operation(owner_storage_budget,'acquire','owner.acquire')
timed_operation(sessions.Session,'prepare','session.prepare')
'''


def verify(configuration, review_path, database_url=None):
    review = json.loads(review_path.read_text(encoding='utf8'))
    for name, digest in review['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    api = IsolatedServer('live-cli-recovery-ui', app='live_fixture:app', database_url=database_url)
    store = ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed')
    report = {'passed': False, 'provider': 'deepseek', 'model': configuration['model'],
              'mock_llm_calls': 0, 'simulated_llm': False, 'actual_CLI_turns_started': 0,
              'CLI_internal_HTTP_count_observed': False, 'checks': [], 'review': str(review_path),
              'store_root': str(store.root), 'developer_database_modified': False,
              'database': 'postgresql' if database_url else 'sqlite'}
    preview = log = None

    def save():
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        assert configuration['key'] not in encoded
        (api.root/'live-result.json').write_text(encoded+'\n', encoding='utf8')

    def call(method, path, body=None):
        until = time.monotonic()+30
        with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                          timeout=180, trust_env=False) as client:
            while True:
                response = client.request(method, path, json=body)
                detail = response.json().get('detail') if response.status_code == 409 else None
                if not isinstance(detail, dict) or detail.get('code') != 'storage_quota_busy' or time.monotonic() >= until:
                    break
                report.setdefault('quota_retries', []).append({'method': method, 'path': path}); save(); time.sleep(.25)
            assert response.status_code == 200, (method, path, response.status_code,
                response.text.replace(configuration['key'], '[REDACTED]')[:600])
            return response.json()

    def gate(name):
        until = time.monotonic()+900
        while not (api.root/name).exists():
            assert preview.poll() is None and api.proc.poll() is None
            if time.monotonic() >= until: raise TimeoutError('Browser did not finish '+name)
            time.sleep(.2)
        value = json.loads((api.root/name).read_text(encoding='utf8'))
        assert value['passed'], value
        return value

    print('LIVE_CLI_RECOVERY_UI '+str(api.root), flush=True)
    save()
    try:
        with store:
            settings = 'import os\nos.environ.update('+repr(store.settings())+')\n'
            (api.root/'live_fixture.py').write_text(settings+LIVE+MANAGED+INFERENCE+APPROVAL+DIAGNOSTICS, encoding='utf8')
            with api:
                api.create_case('CLI recovery browser acceptance')
                user = call('GET', '/auth/me')
                call('POST', '/test/prepare-cli-inference')
                profile = call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Actual recovery UI',
                    'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': configuration['key']})
                conv = call('POST', '/conversations', {'case_id': api.case_id, 'title': 'Recovery UI actual conversation',
                    'permission_mode': 'ask', 'provider_profile_id': profile['id'], 'model': configuration['model']})
                call('PUT', api.prefix+'/file', {'path': 'Ref/recovery-proof.txt', 'content': 'ORIGINAL'})
                session = call('POST', '/codex/sessions', {'case_id': api.case_id, 'conversation_id': conv['id'],
                    'runtime': 'wsl', 'provider_profile_id': profile['id'], 'provider_wire_api': 'responses'})
                turn = call('POST', '/codex/sessions/'+session['id']+'/turns', {'content':
                    '请现在实际调用一次命令工具执行 cat Ref/recovery-proof.txt。不要只描述计划或承诺执行；平台会暂停工具等待审批。不要使用其他工具或读取方式。',
                    'client_message_id': uuid.uuid4().hex, 'timeout_seconds': 120})
                report['actual_CLI_turns_started'] = 1; report['old_turn'] = turn; save()
                until = time.monotonic()+150
                while True:
                    pending = [a for a in call('GET', '/codex/turns/'+turn['id']+'/approvals') if a['phase'] == 'pending']
                    if pending: break
                    observed = call('GET', '/codex/turns/'+turn['id'])
                    if observed['phase'] in TERMINAL:
                        report['initial_terminal_turn'] = observed; save()
                        raise AssertionError('Real model turn ended before requesting command approval')
                    assert time.monotonic() < until; time.sleep(.3)
                assert len(pending) == 1 and pending[0]['method'] == 'item/commandExecution/requestApproval'
                manifests = list((api.root/'files/.staging').glob('*.manifest.json')); assert len(manifests) == 1
                stage = manifests[0].with_name(manifests[0].name.removesuffix('.manifest.json'))
                nonce = 'UI_RECOVERED_'+uuid.uuid4().hex
                (stage/'Ref/recovery-proof.txt').write_text(nonce, encoding='utf8')
                report['filesystem_fault_input'] = 'Actual isolated staging edit, not model-generated output'
                try: call('POST', '/test/approval-exit')
                except httpx.TransportError: pass
                else: raise AssertionError('API did not exit')
                old = api.proc; api.__exit__(None, None, None); assert old.poll() is not None
                shutil.copyfile(api.root/'api.log', api.root/'original-api.log')
                api.__enter__()
                api.token = api.call('POST', '/auth/login', {'email': user['email'], 'password': 'isolated-test-123'})['token']
                call('PUT', api.prefix+'/file', {'path': 'Ref/recovery-proof.txt', 'content': 'REMOTE_CHANGE'})
                report['checks'].append('actual CLI approval and API exit leave local/remote conflict'); save()
                with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
                web = ROOT/'platform/web'; log = (api.root/'web-preview.log').open('w', encoding='utf8')
                preview = subprocess.Popen([shutil.which('node.exe') or 'node', str(web/'node_modules/vite/bin/vite.js'),
                    'preview', '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=web,
                    env=dict(os.environ, VITE_API_TARGET=api.base), stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                setup = {'root': str(api.root), 'web': 'http://127.0.0.1:'+str(port), 'email': user['email'],
                    'case_id': api.case_id, 'conversation_id': conv['id'], 'user_id': user['id'], 'turn_id': turn['id'],
                    'expected': nonce, 'profile_id': profile['id']}
                (api.root/'browser-setup.json').write_text(json.dumps(setup, indent=2)+'\n', encoding='utf8')
                print('CLI_RECOVERY_UI_READY '+setup['web'], flush=True)
                report['browser_conflict'] = gate('browser-conflict.json')
                state = call('GET', '/test/approval-state/'+turn['id'])
                assert state['session']['cleanup_verified'] and state['execution']['phase'] == 'recovery_required'
                assert all(a['phase'] == 'expired' for a in state['approvals'])
                assert (stage/'Ref/recovery-proof.txt').read_text() == nonce
                assert call('GET', api.prefix+'/file?path=Ref/recovery-proof.txt')['content'] == 'REMOTE_CHANGE'
                call('PUT', api.prefix+'/file', {'path': 'Ref/recovery-proof.txt', 'content': 'ORIGINAL'})
                (api.root/'browser-retry-ready.json').write_text('{"ready":true}\n', encoding='utf8')
                report['checks'].append('browser recovery stops scope and reports actual conflict without losing files'); save()
                report['browser_final'] = gate('browser-finished.json')
                rows = call('GET', '/codex/conversations/'+str(conv['id'])+'/turns')['data']
                report['actual_CLI_turns_started'] = len(rows)
                assert len(rows) == 2, 'Browser must not replay model turns on reload or recovery'
                final = next(row for row in rows if row['id'] != turn['id'])
                report['final_turn'] = final; save()
                assert final['phase'] == 'completed' and final['cleanup_verified'] is True
                state = call('GET', '/test/approval-state/'+final['id'])
                assert ''.join(e['payload'].get('text', '') for e in state['events'] if e['type'] == 'text.delta') == nonce
                assert any(e['type'] == 'item.completed' and e['payload'].get('type') == 'commandExecution'
                    and e['payload'].get('exitCode') == 0 and nonce in (e['payload'].get('aggregatedOutput') or '') for e in state['events'])
                history = call('GET', '/conversations/'+str(conv['id']))
                assert any(e['type'] == 'message' and e['payload'].get('native_execution_id') == final['id']
                    and e['payload'].get('content') == nonce for e in history['events'])
                assert not stage.exists() and not manifests[0].exists()
                assert call('GET', '/codex/conversations/'+str(conv['id'])+'/activity')['phase'] == 'idle'
                report['checks'].append('browser retry, reload and new real model turn preserve recovered file and exact persisted answer')
                report['functional_passed'] = True; save()
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1600]}
    finally:
        if preview is not None:
            if preview.poll() is None: preview.terminate()
            preview.wait(timeout=10)
            report['preview_exited'] = preview.poll() is not None
        if log is not None: log.close()
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        report['store_stopped'] = store.proc is not None and store.proc.poll() is not None
        report['passed'] = bool(report.get('functional_passed') and report['api_exited'] and report['store_stopped'] and report.get('preview_exited'))
        save()
        print('CLI_RECOVERY_UI_RESULT '+str(api.root/'live-result.json'), flush=True)
    return 0 if report['passed'] else 1


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-profile-id', required=True, type=int)
    parser.add_argument('--review', required=True, type=Path)
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    configuration = await source(args.source_profile_id)
    if not args.postgres:
        return await asyncio.to_thread(verify, configuration, args.review.resolve())
    # Same disposable local database lifecycle as the reviewed backend entry.
    import asyncpg
    import tempfile
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    selected = make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername == 'postgresql+asyncpg' and selected.host in ('localhost', '127.0.0.1', '::1')
    name = 'tmp_vf_clirecoveryui_'+uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix='cli-recovery-ui-postgres-', dir=ROOT/'tmp'))
    report = {'passed': False, 'database': name, 'developer_database_modified': False}
    admin = await asyncpg.connect(host=selected.host, port=selected.port or 5432,
        user=selected.username, password=selected.password, database='postgres', timeout=10)
    created = False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"'); created = True
        url = selected.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic, url, root, 'upgrade', 'head')
        result = await asyncio.to_thread(verify, configuration, args.review.resolve(), url)
        report['passed'] = result == 0
        return result
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
                report['temporary_database_dropped'] = True
        finally:
            await admin.close()
            (root/'result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
            print('CLI_RECOVERY_UI_POSTGRES_RESULT '+str(root/'result.json'), flush=True)


if __name__ == '__main__': raise SystemExit(asyncio.run(main()))
