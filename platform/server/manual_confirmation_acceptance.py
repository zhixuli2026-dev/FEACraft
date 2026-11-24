"""Real files/API death, paged audit and original confirmation continuation.

Optional browser acceptance uses the actual built app and isolated browsers.
There is no inference, provider substitution, or simulated CLI result.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import time
import traceback
import uuid

import httpx
from testsupport import IsolatedServer, SERVER
from manual_file_recovery_acceptance import APP as BASE

ROOT = SERVER.parents[1]
APP = BASE + '''
@app.get('/api/v1/test/confirmation-terminals')
async def terminals(user=Depends(get_current_user)):
    from app.models import NativeSession
    from app.routers.terminal import _sessions
    from app.services.terminal_records import public
    async with SessionLocal() as db:
        return [dict(public(row),owner_available=row.id in _sessions) for row in await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id))
            if json.loads(row.owner_identity_json)==observe(os.getpid())]

@app.post('/api/v1/test/confirmation-share/{case_id}')
async def share(case_id:int,body:dict,user=Depends(get_current_user)):
    from app.models import Membership,Workspace
    from app.services import quota,sharing
    async with SessionLocal() as db:
        case=await get_owned_case(case_id,user,db,write=True)
        workspace=await db.get(Workspace,case.workspace_id)
        assert workspace.user_id==user.id
        org=await quota.ensure_user_org(db,user)
        row=await db.scalar(select(Membership).where(Membership.user_id==body['user_id'],Membership.org_id==org.id))
        if row is None:row=Membership(user_id=body['user_id'],org_id=org.id,role=body['role']);db.add(row)
        row.role=body['role']
        await sharing.set_share(db,workspace,org.id,'member',True)
        await db.commit()
        return {'role':row.role}
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256'] == hashlib.sha256(APP.encode()).hexdigest()
    for name, digest in review['source_hashes'].items():
        assert sha(ROOT / name) == digest, name
    api = IsolatedServer('manual-confirmation', app='confirmation_fixture:app')
    (api.root / 'confirmation_fixture.py').write_text(APP, encoding='utf8')
    report = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0,
              'review_sha256': sha(args.review), 'checks': [], 'API_exits': []}
    entered = False
    handles = []
    preview = preview_handle = preview_log = None

    def witness():
        birth = api.call('GET', '/test/manual-birth')
        handle = ProcessHandle.query(birth)
        handles.append((handle, birth))
        return handle, birth

    def options(source):
        return api.call('GET', api.prefix + '/file-recovery?source=' + source)

    def body(plan, action, **extra):
        return dict(action=action, source=plan.get('source', 'pending'), snapshot_sha256=plan['snapshot_sha256'],
                    expected_revision=plan['expected_revision'], expected_event_sequence=plan['expected_event_sequence'],
                    note='Actual portable confirmation ' + action, **extra)

    def send(value):
        return api.call('POST', api.prefix + '/file-recovery/actions', value,
                        extra_headers={'Idempotency-Key': str(uuid.uuid4())}, expect=202)

    def detail(ticket):
        return api.call('GET', api.prefix + '/file-recovery/actions/' + ticket)

    def resume(ticket, digest, expect=202):
        return api.call('POST', api.prefix + '/file-recovery/actions/' + ticket + ':continue',
                        {'request_sha256': digest, 'confirmed': True}, expect=expect)

    try:
        api.__enter__(); entered = True; handle, birth = witness()
        api.create_case('Portable manual confirmation')
        user = api.call('GET', '/auth/me'); owner = api.token
        api.put('Ref/current.txt', 'CURRENT_INITIAL')
        sources = [api.call('POST', '/test/manual-seed/' + str(api.case_id), {'kind': 'preparing'})['source'] for _ in range(23)]
        first = send(body(options(sources[0]), 'isolate_version'))
        restore = api.call('GET', api.prefix + '/file-recovery/actions/' + first['id'] + '/restore-plan?path=before/0')
        version = api.call('GET', api.prefix + '/file:version?path=Ref/current.txt')
        restored = send(body(restore, 'restore_file', isolation_id=first['id'], file_path='before/0',
                             target_path='Ref/current.txt', expected_target_sha256=version['content_sha256']))
        api.put('Ref/current.txt', 'NEWER_EDIT_MUST_SURVIVE')
        saved = detail(restored['id'])
        assert resume(restored['id'], saved['confirmation']['request_sha256']) == restored
        assert api.call('GET', api.prefix + '/raw?path=Ref/current.txt', raw=True) == b'NEWER_EDIT_MUST_SURVIVE'
        report['checks'].append({'kind': 'completed_restore_original_receipt_keeps_newer_edit', 'ticket': restored['id']})
        for _ in range(20):
            send(body(options(sources[1]), 'retain_for_review'))
        rows = []; cursor = None
        while True:
            page = api.call('GET', api.prefix + '/file-recovery/actions-page?limit=7' + ('' if cursor is None else '&before_seq=' + str(cursor)))
            rows += page['items']; cursor = page['next_before_seq']
            if cursor is None: break
        assert len(rows) == 22 and len({row['id'] for row in rows}) == 22
        assert api.call('GET', api.prefix + '/file-recovery/actions') == rows
        preparations = []; cursor = ''
        while True:
            page = api.call('GET', api.prefix + '/file-transactions?include_preparations=true&preparation_limit=7&preparation_after=' + cursor)['preparations']
            preparations += [row['cursor'] for row in page['entries']]
            if not page['has_more']: break
            cursor = page['next_cursor']
        assert sorted(preparations) == sorted(sources[1:]) and len(preparations) == 22
        report['checks'].append({'kind': 'actual_audit_and_preparation_paging', 'audit_rows': 22, 'preparation_rows': 22,
                                 'audit_pages': 4, 'preparation_pages': 4, 'no_duplicates': True})
        pending = api.call('POST', '/test/manual-seed/' + str(api.case_id), {'kind': 'pending', 'exit_after_move': True})['source']
        request = body(options(pending), 'isolate_version')
        try: send(request)
        except OSError: pass
        else: raise AssertionError('Expected actual API death after physical move')
        handle.wait_exit(15); report['API_exits'].append({'birth': birth, 'proof': 'retained_creation_handle'})
        api.__exit__(None, None, None); entered = False
        shutil.copyfile(api.root / 'api.log', api.root / 'api-before-exit.log')
        api.__enter__(); entered = True; handle, birth = witness()
        api.token = api.call('POST', '/auth/login', {'email': user['email'], 'password': 'isolated-test-123'})['token']
        owner = api.token
        pending_row = api.call('GET', api.prefix + '/file-recovery/actions-page?limit=1')['items'][0]
        assert pending_row['state'] == 'pending' and pending_row['action'] == 'isolate_version'
        ticket = pending_row['id']; saved = detail(ticket); digest = saved['confirmation']['request_sha256']
        assert saved['confirmation']['can_continue'] is True
        assert saved['confirmation']['request']['note'] == request['note']
        assert resume(ticket, '0' * 64, expect=409)['detail']['code'] == 'idempotency_conflict'
        assert resume(str(uuid.uuid4()), digest, expect=404)
        foreign = api.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com', 'password': 'isolated-test-123'})
        api.token = foreign['token']; assert resume(ticket, digest, expect=404)
        api.token = owner
        api.call('POST', '/test/confirmation-share/' + str(api.case_id), {'user_id': foreign['user']['id'], 'role': 'viewer'})
        api.token = foreign['token']; assert detail(ticket)['confirmation']['can_continue'] is False
        assert resume(ticket, digest, expect=404)
        api.token = owner
        api.call('POST', '/test/confirmation-share/' + str(api.case_id), {'user_id': foreign['user']['id'], 'role': 'member'})
        api.token = foreign['token']; assert detail(ticket)['confirmation']['can_continue'] is False
        assert resume(ticket, digest, expect=403)
        api.token = owner
        report['checks'].append({'kind': 'original_actor_and_digest_required_before_continuation',
                                 'wrong_digest': 409, 'unknown_ticket': 404, 'foreign': 404, 'viewer': 404, 'other_writer': 403})
        if args.browser:
            with socket.socket() as sock: sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            web = ROOT / 'platform/web'; preview_log = (api.root / 'web-preview.log').open('w', encoding='utf8')
            preview = subprocess.Popen([shutil.which('node.exe') or 'node', str(web / 'node_modules/vite/bin/vite.js'), 'preview',
                '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=web,
                env=dict(os.environ, VITE_API_TARGET=api.base), stdout=preview_log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            preview_birth = identity.observe(preview.pid); preview_handle = ProcessHandle.query(preview_birth)
            report['preview_birth'] = preview_birth
            web_url = 'http://127.0.0.1:' + str(port)
            with httpx.Client(timeout=1, trust_env=False) as client:
                until = time.monotonic() + 30
                while True:
                    assert preview.poll() is None and time.monotonic() < until
                    try:
                        if client.get(web_url).status_code == 200: break
                    except httpx.HTTPError: pass
                    time.sleep(.1)
            setup = {'web': web_url, 'root': str(api.root), 'email': user['email'], 'case_id': api.case_id,
                     'case_name': 'Portable manual confirmation', 'ticket': ticket, 'request_sha256': digest}
            (api.root / 'browser-setup.json').write_text(json.dumps(setup, indent=2), encoding='utf8')
            print('MANUAL_CONFIRMATION_BROWSER_READY ' + str(api.root), flush=True)
            done = api.root / 'browser-finished.json'; until = time.monotonic() + 1200
            while not done.exists():
                assert preview.poll() is None and api.proc.poll() is None
                if time.monotonic() >= until: raise TimeoutError('Browser acceptance not finished')
                time.sleep(.2)
            report['browser'] = json.loads(done.read_text(encoding='utf8'))
            assert report['browser']['passed']
        completed = resume(ticket, digest)
        assert completed['id'] == ticket and completed['state'] == 'completed'
        assert resume(ticket, digest) == completed
        assert api.call('GET', api.prefix + '/file-recovery/actions/' + ticket + '/file?path=before/0', raw=True) == b'actual-retained-before'
        assert api.call('GET', api.prefix + '/raw?path=Ref/current.txt', raw=True) == b'NEWER_EDIT_MUST_SURVIVE'
        with sqlite3.connect(api.root / 'test.sqlite') as db:
            assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
            events = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM durable_events WHERE type='files.manual_recovery.committed'")]
            assert sum(row['ticket_id'] == ticket for row in events) == 1
            assert not db.execute("SELECT id FROM case_storage_operations WHERE state!='settled'").fetchall()
        report['checks'].append({'kind': 'actual_exit_original_ticket_completed_once', 'ticket': ticket,
                                 'one_commit': True, 'newer_edit_preserved': True, 'no_jobs': True})
        report['functional_passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:1800],
                             'frames': [{'file': Path(row.filename).name, 'line': row.lineno}
                                        for row in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if args.browser:
                    for row in api.call('GET', '/test/confirmation-terminals'):
                        if not row['cleanup_verified'] and row['owner_available']:
                            api.call('DELETE', '/terminal/sessions/' + row['id'])
                    terminals = api.call('GET', '/test/confirmation-terminals')
                    assert all(row['cleanup_verified'] for row in terminals)
                    report['actual_terminal_stops'] = terminals
            except BaseException as error: report['cleanup_failure'] = type(error).__name__
        if preview: preview.terminate(); preview.wait(timeout=15)
        if preview_handle: preview_handle.wait_exit(15); preview_handle.close(); report['preview_exit_verified'] = True
        if preview_log: preview_log.close()
        if entered:
            api.__exit__(None if report.get('functional_passed') else RuntimeError, None, None)
            handle.wait_exit(15); report['API_exits'].append({'birth': birth, 'proof': 'retained_creation_handle'})
        for item, _ in handles: item.close()
        report['passed'] = bool(report.get('functional_passed') and len(report['API_exits']) == 2
                                and 'cleanup_failure' not in report)
        (api.root / 'confirmation-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print('MANUAL_CONFIRMATION_RESULT ' + str(api.root / 'confirmation-result.json'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
