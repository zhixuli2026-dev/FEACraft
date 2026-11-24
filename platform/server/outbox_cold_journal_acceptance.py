"""Drive real index delivery through a file journal left by an exited API."""
import argparse
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import time

import httpx

from file_transaction_diagnostics_acceptance import FIXTURE, ORIGINAL, SIZE, TARGET, arm, restart, seed
from s3testsupport import ObjectStore
from sandbox_normal_close_resource_smoke import RUNTIME, writer_available
from testsupport import IsolatedServer


INDEXED = 'Ref/outbox-proof.txt'

OUTBOX = r'''
import asyncio
from app.db import SessionLocal
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import durable_events
from app.services.durable_events import append_event, deliver_pending
from fastapi import Depends

dispatch = durable_events.dispatch_forever


async def gated_dispatch(factory, stop, **options):
    while not (root/'dispatch-now').exists() and not stop.is_set():
        await asyncio.sleep(.05)
    await dispatch(factory, stop, **options)


durable_events.dispatch_forever = gated_dispatch


@app.post('/fixture/outbox-drain')
async def drain(user=Depends(get_current_user)):
    return {'delivered': await deliver_pending(SessionLocal)}


@app.post('/fixture/outbox/{case_id}')
async def publish_later(case_id: int, user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case = await get_owned_case(case_id, user, db)
        event = await append_event(db, case.id, 'files.changed',
            {'paths': ['Ref/outbox-proof.txt'], 'changes': [], 'rescan': True, 'paths_complete': True})
        await db.commit()
        return {'id': event.id}
'''


def receipt(api, identity):
    path = (api.root/'test.sqlite').resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(path, uri=True)) as db:
        row = db.execute('SELECT status,claim_token,lease_expires_at,attempts,last_error '
                         'FROM event_outbox WHERE event_id=?', (identity,)).fetchone()
    return dict(zip(('status', 'claim', 'lease', 'attempts', 'error'), row))


def observe(api, path, timeout=40):
    end = time.monotonic()+timeout
    while not path.exists():
        assert api.proc.poll() is None
        if time.monotonic() >= end:
            raise TimeoutError(f'Outbox did not start recovery in {api.root}')
        time.sleep(.05)
    return json.loads(path.read_text(encoding='utf8'))


def wait_for_delivery(api, identity, seconds):
    end = time.monotonic()+seconds
    while True:
        value = receipt(api, identity)
        if value['status'] == 'delivered':
            return value
        if time.monotonic() >= end:
            raise AssertionError(value)
        assert api.proc.poll() is None
        time.sleep(.08)


def verify(backend, store, *, kill_recovery=False):
    api = IsolatedServer('outbox-local-journal-'+backend, app='outbox_journal_fixture:app')
    fixture = api.root/'outbox_journal_fixture.py'
    if store is not None:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    fixture.write_text('import os\nos.environ.update('+repr(store.settings() if store else {})+')\n'
                       +FIXTURE+OUTBOX, encoding='utf8')
    report = {'passed': False, 'backend': backend, 'actual_model_calls': 0, 'mock_llm_calls': 0}
    try:
        with api:
            api.create_case('Durable indexing after cold journal')
            workspace = api.call('GET', '/workspaces')[0]['id']
            other = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Independent case'})['id']
            api.call('PUT', api.prefix+'/file', {'path': INDEXED, 'content': 'outboxrecoveryword'})
            api.call('POST', api.prefix+'/index:rebuild')
            api.call('PUT', f'/cases/{other}/file', {'path': INDEXED, 'content': 'independentindexword'})
            api.call('POST', f'/cases/{other}/index:rebuild')
            api.call('POST', '/fixture/outbox-drain', api_prefix=False)
            identity = api.call('POST', f'/fixture/outbox/{api.case_id}', api_prefix=False)['id']
            report['event_id'] = identity
            assert receipt(api, identity)['status'] == 'pending' and receipt(api, identity)['attempts'] == 0
            seed(api, store)
            with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                              trust_env=False, timeout=100) as client:
                (api.root/'arm-exit').touch()
                try:
                    client.put(api.prefix+'/file', json={'path': TARGET, 'content': 'uncommitted object'})
                except httpx.TransportError:
                    pass
                else:
                    raise AssertionError('Expected API exit before the write committed')
                report['restart'] = restart(api, 84)
                arm(api)
                (api.root/'dispatch-now').touch()
                try:
                    report['paused'] = observe(api, api.root/'read-observed.json')
                    assert report['paused']['body_size'] == SIZE and writer_available(api)
                    report['paused']['receipt'] = receipt(api, identity)
                    assert report['paused']['receipt'] == {'status': 'pending', 'claim': None,
                        'lease': None, 'attempts': 1, 'error': 'DeferredFileRecovery'}, report['paused']
                    write = client.put(f'/cases/{other}/file', json={'path': 'Ref/during-recovery.txt', 'content': 'saved'})
                    assert write.status_code == 200, write.text
                    report['paused']['independent_save_http'] = 200
                    unrelated = api.call('POST', f'/fixture/outbox/{other}', api_prefix=False)['id']
                    hold_until = time.monotonic()+10.3
                    report['paused']['independent_event'] = wait_for_delivery(api, unrelated, seconds=12)
                    time.sleep(max(0, hold_until-time.monotonic()))
                    assert receipt(api, identity) == report['paused']['receipt']
                    report['paused']['held_longer_than_delivery_deadline'] = True
                    if kill_recovery:
                        api.proc.kill()
                        killed = api.proc.wait(timeout=10)
                        report['second_restart'] = restart(api, killed)
                        assert receipt(api, identity) == report['paused']['receipt']
                finally:
                    (api.root/'release-read').touch()
                report['delivered'] = wait_for_delivery(api, identity, seconds=65)
                assert report['delivered']['attempts'] == (3 if kill_recovery else 2), report
                index = api.root/'index'/f'case-{api.case_id}.sqlite'
                with closing(sqlite3.connect(index)) as db:
                    assert db.execute('SELECT count(*) FROM consumed_events WHERE event_id=?', (identity,)).fetchone() == (1,)
                result = client.get(api.prefix+'/file:version', params={'path': TARGET})
                assert result.status_code == 200 and result.json()['content_sha256'] == ORIGINAL, result.text
                report['restored_sha256'] = ORIGINAL
                assert client.get(api.prefix+'/file-transactions').json()['status'] == 'idle'
                hits = api.call('GET', api.prefix+'/search?q=outboxrecoveryword')['hits']
                assert hits and hits[0]['path'] == INDEXED, hits
                report['index_effect_count'] = 1
        report['passed'] = True
    finally:
        fixture.unlink(missing_ok=True)
        (api.root/'outbox-cold-journal.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print('OUTBOX_COLD_JOURNAL_PASS', backend, api.root, flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('fs', 's3'), required=True)
    parser.add_argument('--kill-recovery', action='store_true')
    args = parser.parse_args()
    context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if args.backend == 's3' else nullcontext()
    with context as store:
        verify(args.backend, store, kill_recovery=args.kill_recovery)
