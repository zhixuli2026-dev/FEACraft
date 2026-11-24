"""Actual cmd PTY, SQLite contention and API-exit completion recovery.

This isolated infrastructure entry invokes no model or inference substitute.
Its fixture only observes actual close calls and original pending receipts.
The parent holds a real SQLite writer, then terminates the original API after
the native stop, keeping the encrypted journal and database for the peer API.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
import traceback

from testsupport import IsolatedServer, SERVER
from websockets.sync.client import connect

ROOT = SERVER.parents[1]
APP = '''import os
from fastapi import Depends,HTTPException
from app.main import app
from app.deps import get_current_user
from app.services.process_identity_native import observe
from app.services.terminal_sessions import TerminalSession
from app.routers import terminal
counts={}
original_close=TerminalSession._close
async def close(self,*args,**kwargs):
 counts[self.id]=counts.get(self.id,0)+1
 return await original_close(self,*args,**kwargs)
TerminalSession._close=close
@app.get('/api/v1/test/completion-birth')
async def birth():return observe(os.getpid())
@app.get('/api/v1/test/completion-observation/{sid}')
async def observation(sid:str,user=Depends(get_current_user)):
 session=terminal._sessions.get(sid)
 if session is None or session.user_id!=user.id:raise HTTPException(404)
 payload=session._pending_completion[1] if session._pending_completion else None
 return {'close_calls':counts.get(sid,0),'pending_payload':payload,
  'actual_stop_observed':session.cleanup_verified,'persisted':session.persisted_terminal}
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    assert review['entry'] == 'platform/server/terminal_completion_acceptance.py'
    assert review['mock_llm_calls'] == 0 and review['real_model_calls_expected'] == 0
    for name, digest in review['source_hashes'].items():
        assert sha(ROOT / name) == digest, name
    ast.parse(APP)
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity, process_stop
    api = IsolatedServer('terminal-completion', app='terminal_completion_fixture:app')
    (api.root / 'terminal_completion_fixture.py').write_text(APP, encoding='utf8')
    result = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0,
              'review_sha256': sha(args.review), 'scenarios': {}, 'API_births': []}
    entered = False
    peer = peer_log = lock = None
    handles = []
    owned = []
    original_base = api.base
    peer_base = None

    def wait(predicate, seconds=15):
        end = time.monotonic() + seconds
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= end:
                raise TimeoutError('Same actual operation did not reach its observation boundary')
            time.sleep(.05)

    def native():
        socket_url = api.base.replace('http://', 'ws://') + '/ws/terminal?shell=cmd&token=' + api.token
        ws = connect(socket_url, open_timeout=30, max_size=2 * 1024 * 1024, legacy=True)
        try:
            end = time.monotonic() + 40
            while True:
                item = ws.recv(timeout=max(.01, end - time.monotonic()))
                if isinstance(item, str):
                    data = json.loads(item)
                    if data['type'] == 'error':
                        raise RuntimeError('Actual PTY startup failed: ' + str(data.get('code')))
                    if data['type'] == 'ready':
                        owned.append((data['session'], api.base))
                        return ws, data['session']
        except BaseException:
            ws.close()
            raise

    try:
        api.__enter__()
        entered = True
        birth = api.call('GET', '/test/completion-birth')
        result['API_births'].append(birth)
        handles.append(ProcessHandle.query(birth))
        api.create_case('Actual terminal completion recovery')
        owner_token = api.token
        # Start a second actual API with the same configured key and private
        # audit directory before taking the writer lock. No test credentials
        # or owner capabilities are printed or copied into result documents.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            peer_port = sock.getsockname()[1]
        peer_base = 'http://127.0.0.1:' + str(peer_port)
        peer_log = (api.root / 'peer-api.log').open('w', encoding='utf8')
        peer = subprocess.Popen([sys.executable, '-m', 'uvicorn', api.app, '--app-dir', str(SERVER),
            '--host', '127.0.0.1', '--port', str(peer_port), '--no-access-log'],
            cwd=api.root, env=api.env.copy(), stdout=peer_log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        api.base = peer_base
        def peer_ready():
            if peer.poll() is not None:
                raise RuntimeError('Actual peer API exited during startup')
            try:return api.call('GET', '/api/health', api_prefix=False).get('ok')
            except (OSError, ValueError):return False
        wait(peer_ready, 30)
        birth = api.call('GET', '/test/completion-birth')
        result['API_births'].append(birth)
        handles.append(ProcessHandle.query(birth))
        # A real second user must not be able to inspect or recover the first
        # user's terminal, even when a retained completion exists.
        foreign_token = api.call('POST', '/auth/register', {
            'email': 'foreign-' + api.root.name + '@example.com',
            'password': 'isolated-test-123', 'name': 'Other terminal owner'})['token']
        api.token = owner_token
        api.base = original_base
        ws, sid = native()
        marker = 'ACTUAL_TERMINAL_COMPLETION_' + sid.replace('-', '')
        executed = api.root / 'actual-pty-executions.txt'
        ws.send(json.dumps({'type': 'input', 'data': 'echo ' + marker + '>>"' + str(executed) + '"&type "' + str(executed) + '"\r'}))
        output = ''
        end = time.monotonic() + 20
        while True:
            frame = ws.recv(timeout=max(.01, end - time.monotonic()))
            if isinstance(frame, bytes):
                output += frame.decode('utf8', 'replace')
                plain = re.sub(r'\x1b\].*?(?:\x07|\x1b\\)', '', output, flags=re.S)
                plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', plain)
                if any(line.strip() == marker for line in plain.splitlines()):break
        ws.close()
        assert executed.read_text(encoding='utf8').splitlines() == [marker]
        lock = sqlite3.connect(api.root / 'test.sqlite', timeout=5)
        lock.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        accepted = api.call('DELETE', '/terminal/sessions/' + sid + '?wait=false', expect=202)
        accepted_ms = round((time.monotonic() - started) * 1000, 3)
        assert accepted['accepted'] is True and accepted_ms < 5000
        def retained():
            value = api.call('GET', '/terminal/sessions/' + sid)
            return value if value['completion_retained'] and value['actual_stop_observed'] is True else None
        pending = wait(retained)
        assert pending['completion_pending'] and pending['cleanup_verified'] is None
        assert pending['phase'] in {'active', 'recovery_required'}
        observation = api.call('GET', '/test/completion-observation/' + sid)
        assert observation['close_calls'] == 1 and not observation['persisted']
        original = observation['pending_payload']
        process_stop.validate(original['stop'], original['root'])
        assert original['stop']['verified'] is True
        assert identity.probe(original['root']['host'])['state'] == 'exited'
        witness = api.root / 'audit/terminal-completions' / (sid + '.enc')
        encrypted = witness.read_bytes()
        assert encrypted and not encrypted.startswith(b'{')
        api.base = peer_base
        refused = api.call('DELETE', '/terminal/sessions/' + sid, expect=409)
        assert refused['detail']['code'] == 'completion_owner_alive'
        api.token = foreign_token
        api.call('DELETE', '/terminal/sessions/' + sid, expect=404)
        api.token = owner_token
        altered = bytearray(encrypted)
        altered[-10] ^= 1
        try:
            witness.write_bytes(altered)
            refused_tamper = api.call('DELETE', '/terminal/sessions/' + sid, expect=409)
            assert refused_tamper['detail']['code'] == 'completion_witness_unverifiable'
        finally:
            witness.write_bytes(encrypted)
        with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT phase,stop_json FROM native_sessions WHERE id=?', (sid,)).fetchone() == ('active', '{}')
        # The original API really exits while its SQL completion is blocked.
        # The native command has already stopped; no shell is relaunched.
        api.proc.terminate()
        api.proc.wait(timeout=15)
        handles[0].wait_exit(15)
        assert identity.probe(result['API_births'][0])['state'] == 'exited'
        assert witness.read_bytes() == encrypted
        lock.rollback()
        lock.close()
        lock = None
        recovered = api.call('DELETE', '/terminal/sessions/' + sid)
        assert recovered['ok'] is True
        final = api.call('GET', '/terminal/sessions/' + sid)
        assert final['phase'] == 'closed' and final['cleanup_verified'] is True
        assert final['stop'] == original['stop'] and final['process']['root'] == original['root']
        assert final['tail'] == original['tail'] and not final['completion_pending']
        assert not witness.exists()
        api.call('DELETE', '/terminal/sessions/' + sid)
        assert executed.read_text(encoding='utf8').splitlines() == [marker]
        with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT count(*) FROM native_sessions').fetchone()[0] == 1
        result['scenarios']['SQL_lock_and_API_exit'] = {
            'passed': True, 'session_id': sid, 'accepted_ms': accepted_ms,
            'actual_close_calls': observation['close_calls'], 'pending_phase': pending['phase'],
            'no_false_persisted_cleanup': pending['cleanup_verified'] is None,
            'actual_stop': original['stop'], 'original_API_exit_verified': True,
            'owner_alive_refused': True, 'foreign_owner_refused': True, 'tampered_witness_refused': True,
            'encrypted_witness_bytes': len(encrypted), 'encrypted_witness_sha256': hashlib.sha256(encrypted).hexdigest(),
            'same_original_root_stop_and_tail': True, 'actual_command_execution_count': 1,
            'completion_acknowledged': True}
        # A real filesystem obstruction must not prevent a valid SQL close.
        ws, normal_sid = native()
        ws.close()
        directory = witness.parent
        assert directory.resolve().is_relative_to(api.root.resolve())
        directory.rmdir()
        directory.write_bytes(b'Actual isolated path obstruction')
        try:
            api.call('DELETE', '/terminal/sessions/' + normal_sid)
            normal = api.call('GET', '/terminal/sessions/' + normal_sid)
            assert normal['phase'] == 'closed' and normal['cleanup_verified'] is True
        finally:
            directory.unlink()
            directory.mkdir()
        result['scenarios']['journal_unavailable_SQL_available'] = {
            'passed': True, 'session_id': normal_sid, 'actual_stop': normal['stop'], 'SQL_completion_verified': True}
        result['functional_passed'] = True
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'message': str(error)[:500],
            'frames': [{'file': Path(x.filename).name, 'line': x.lineno} for x in traceback.extract_tb(error.__traceback__)]}
    finally:
        if lock is not None:
            lock.rollback()
            lock.close()
        cleanup = []
        if entered:
            for sid, owner_base in owned:
                bases = [owner_base, peer_base, original_base]
                verified = False
                for base in dict.fromkeys(x for x in bases if x):
                    api.base = base
                    try:
                        state = api.call('GET', '/terminal/sessions/' + sid)
                        if state['cleanup_verified'] is not True:
                            api.call('DELETE', '/terminal/sessions/' + sid)
                            state = api.call('GET', '/terminal/sessions/' + sid)
                        verified = state['cleanup_verified'] is True
                        if verified:break
                    except Exception:pass
                cleanup.append({'session_id': sid, 'actual_stop_verified': verified})
            if peer is not None:
                if peer.poll() is None:peer.terminate()
                peer.wait(timeout=15)
            if peer_log is not None:peer_log.close()
            api.base = original_base
            api.__exit__(None if result.get('functional_passed') else RuntimeError, None, None)
        result['terminal_cleanup'] = cleanup
        result['all_actual_stops_verified'] = bool(cleanup) and all(x['actual_stop_verified'] for x in cleanup)
        for handle in handles:
            handle.wait_exit(15)
            handle.close()
        result['API_exits_verified'] = len(handles) == 2 and all(identity.probe(x)['state'] == 'exited' for x in result['API_births'])
        result['passed'] = bool(result.get('functional_passed') and result['all_actual_stops_verified'] and result['API_exits_verified'])
        path = api.root / 'terminal-completion-result.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print('TERMINAL_COMPLETION_RESULT ' + str(path), flush=True)
        print(json.dumps({'passed': result['passed'], 'scenarios': list(result['scenarios']),
            'failure': result.get('failure'), 'all_actual_stops_verified': result['all_actual_stops_verified'],
            'API_exits_verified': result['API_exits_verified']}), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(run())
