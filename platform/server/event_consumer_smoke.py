"""Durable index effects across API death, redelivery and transaction failure."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from job_owner_startup_smoke import shared
from job_delivery_recovery_smoke import kill_owner
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic

FIXTURE = '''import os
os.environ.update(OVERRIDES)
import asyncio,json,time
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from app.services import durable_events,indexsvc
ROOT=Path(__file__).parent
original_rebuild=indexsvc._rebuild_locked
def rebuild(conn,prefix,building,**options):
 result=original_rebuild(conn,prefix,building,**options)
 if (ROOT/'fail-index').exists():raise OSError('Injected projection failure before commit')
 return result
indexsvc._rebuild_locked=rebuild
original_loop=durable_events.dispatch_forever
async def loop(factory,stop,**kwargs):
 if PAUSED:await stop.wait()
 else:await original_loop(factory,stop,**kwargs)
durable_events.dispatch_forever=loop
original_publish=durable_events.publish_to_local_bus
async def publish(event,factory):
 await original_publish(event,factory)
 selected=ROOT/'hold-job'
 if event.type=='jobs.updated' and json.loads(event.payload_json).get('status')=='succeeded' and selected.exists():
  if json.loads(event.payload_json)['job_id']==int(selected.read_text()):
   (ROOT/'consumed.json').write_text(json.dumps({'event_id':event.id,'case_id':event.case_id}))
   await asyncio.sleep(60)
durable_events.publish_to_local_bus=publish
from app.main import app
from app.db import SessionLocal
from app.models import User,DurableEvent,EventOutbox
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/fixture/birth')
async def birth(user:User=Depends(get_current_user)):
 from app.services.process_identity_native import observe
 return observe(os.getpid())
@app.get('/api/v1/fixture/events/{case_id}')
async def events(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  await get_owned_case(case_id,user,db)
  pairs=await db.execute(select(DurableEvent,EventOutbox).join(EventOutbox,EventOutbox.event_id==DurableEvent.id).where(DurableEvent.case_id==case_id))
  return [{'id':e.id,'type':e.type,'payload':json.loads(e.payload_json),'status':o.status,'attempts':o.attempts,'error':o.last_error} for e,o in pairs]
@app.get('/api/v1/fixture/projection/{case_id}')
async def projection(case_id:int,q:str,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  await get_owned_case(case_id,user,db,write=False)
 return await asyncio.to_thread(indexsvc.search,case_id,q)
'''


def new_api(label, url=None, overrides=None, *, paused=False):
    api = IsolatedServer(label, app='consumer_fixture:app', database_url=url)
    (api.root / 'consumer_fixture.py').write_text(FIXTURE.replace('OVERRIDES', repr(overrides or {}))
                                                 .replace('PAUSED', repr(paused)), encoding='utf8')
    return api


def common(api):
    return {**shared(api), 'VIBEFLOW_INDEX_ROOT': api.env['VIBEFLOW_INDEX_ROOT']}


def until(operation, predicate, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = operation()
        if predicate(result):
            return result
        time.sleep(.1)
    raise AssertionError(result)


def verify(url=None):
    checks = []
    def passed(name):
        checks.append(name)
        print('PASS ' + name, flush=True)
    def projection(api, case_id, word):
        # The public search repairs its own index. Read the existing projection
        # so these checks can attribute changes to the actual outbox consumer.
        return api.call('GET', f'/fixture/projection/{case_id}?q={word}')
    api = new_api('consumer-before', url, paused=True)
    with api:
        api.create_case('Index delivery after owner exit')
        api.call('POST', api.prefix + '/index:rebuild')
        api.put('edited.txt', 'editorword')
        script = api.root / 'payload.py'
        script.write_text('from pathlib import Path\nPath("D-post/result.txt").write_text("persistentword")\n', encoding='utf8')
        job = api.call('POST', api.prefix + '/jobs', {'type': 'convert',
            'command': subprocess.list2cmdline([sys.executable, str(script)])})
        api.wait_job(job['id'])
        assert not projection(api, api.case_id, 'persistentword')['hits']
        pending = api.call('GET', f'/fixture/events/{api.case_id}')
        terminal = next(e for e in pending if e['type'] == 'jobs.updated' and e['payload'].get('status') == 'succeeded')
        changed = next(e for e in pending if e['type'] == 'files.changed' and 'edited.txt' in e['payload']['paths'])
        assert changed['status'] == 'pending'
        assert terminal['status'] == 'pending'
        kill_owner(api, api.call('GET', '/fixture/birth'))
        with new_api('consumer-before-restart', url, common(api)) as peer:
            peer.token = api.token
            until(lambda: projection(peer, api.case_id, 'persistentword'), lambda r: bool(r['hits']))
            until(lambda: peer.call('GET', f'/fixture/events/{api.case_id}'),
                  lambda events: next(e for e in events if e['id'] == terminal['id'])['status'] == 'delivered')
            assert peer.call('GET', f"/jobs/{job['id']}")['status'] == 'succeeded'
            passed('actual_api_death_before_delivery_restores_search_without_websocket_or_new_job')
            index = Path(api.env['VIBEFLOW_INDEX_ROOT']) / f'case-{api.case_id}.sqlite'
            until(lambda: peer.call('GET', f'/fixture/events/{api.case_id}'),
                  lambda events: next(e for e in events if e['id'] == changed['id'])['status'] == 'delivered')
            with sqlite3.connect(index) as db:
                assert db.execute('SELECT count(*) FROM consumed_events WHERE event_id=?', (changed['id'],)).fetchone()[0] == 1
            passed('file_write_transaction_leaves_durable_refresh_consumed_after_restart')
            # Consecutive completions must not hit the removed 30-second skip.
            (peer.root / 'fail-index').touch()
            script.write_text('from pathlib import Path\nPath("D-post/second.txt").write_text("secondword")\n', encoding='utf8')
            second = peer.call('POST', api.prefix + '/jobs', {'type': 'convert',
                'command': subprocess.list2cmdline([sys.executable, str(script)])})
            peer.wait_job(second['id'])
            failures = until(lambda: peer.call('GET', f'/fixture/events/{api.case_id}'), lambda es: any(
                e['type'] == 'jobs.updated' and e['payload'].get('job_id') == second['id'] and e['error'] == 'OSError' for e in es))
            failed = next(e for e in failures if e['type'] == 'jobs.updated' and
                          e['payload'].get('job_id') == second['id'] and e['error'] == 'OSError')
            assert failed['status'] == 'pending'
            assert not projection(peer, api.case_id, 'secondword')['hits']
            with sqlite3.connect(index) as db:
                assert db.execute('SELECT count(*) FROM consumed_events WHERE event_id=?', (failed['id'],)).fetchone()[0] == 0
            (peer.root / 'fail-index').unlink()
            until(lambda: projection(peer, api.case_id, 'secondword'), lambda r: bool(r['hits']))
            passed('failed_consumer_is_not_acked_and_retries_after_atomic_index_rollback')
            passed('consecutive_job_completions_both_refresh_search')

    api = new_api('consumer-after', url)
    with api:
        api.create_case('Index committed before outbox acknowledgement')
        api.call('POST', api.prefix + '/index:rebuild')
        release = api.root / 'release'
        script = api.root / 'payload.py'
        script.write_text('from pathlib import Path\nimport time\ndeadline=time.monotonic()+45\n'
            f'while not Path({str(release)!r}).exists() and time.monotonic()<deadline:time.sleep(.02)\n'
            'Path("D-post/atomic.txt").write_text("atomicword")\n', encoding='utf8')
        job = api.call('POST', api.prefix + '/jobs', {'type': 'convert',
            'command': subprocess.list2cmdline([sys.executable, str(script)])})
        (api.root / 'hold-job').write_text(str(job['id']), encoding='ascii')
        release.touch()
        api.wait_job(job['id'])
        wait_file(api.root / 'consumed.json')
        event_id = json.loads((api.root / 'consumed.json').read_text())['event_id']
        index = Path(api.env['VIBEFLOW_INDEX_ROOT']) / f'case-{api.case_id}.sqlite'
        with sqlite3.connect(index) as db:
            assert db.execute('SELECT count(*) FROM consumed_events WHERE event_id=?', (event_id,)).fetchone()[0] == 1
            building = db.execute("SELECT value FROM meta WHERE key='building_uuid'").fetchone()[0]
        assert projection(api, api.case_id, 'atomicword')['hits']
        before = next(e for e in api.call('GET', f'/fixture/events/{api.case_id}') if e['id'] == event_id)
        assert before['status'] == 'delivering', before
        kill_owner(api, api.call('GET', '/fixture/birth'))
        with new_api('consumer-after-restart', url, common(api)) as peer:
            peer.token = api.token
            events = until(lambda: peer.call('GET', f'/fixture/events/{api.case_id}'),
                           lambda es: next(e for e in es if e['id'] == event_id)['status'] == 'delivered')
            after = next(e for e in events if e['id'] == event_id)
            assert after['attempts'] == before['attempts'] + 1, after
            with sqlite3.connect(index) as db:
                assert db.execute('SELECT count(*) FROM consumed_events WHERE event_id=?', (event_id,)).fetchone()[0] == 1
                assert db.execute("SELECT value FROM meta WHERE key='building_uuid'").fetchone()[0] == building
            assert peer.call('GET', api.prefix + '/search?q=atomicword')['hits']
            passed('actual_api_death_after_index_commit_reclaims_expired_lease_without_rebuilding')
    return {'passed': True, 'count': len(checks), 'checks': checks, 'developer_database_touched': False}


def index_transactions(report):
    from app.services import indexsvc
    root = report / 'files'
    root.mkdir()
    (root / 'input.txt').write_text('oldword', encoding='utf8')
    checks = []
    with patch.object(indexsvc, 'INDEX_DIR', report / 'index'), \
         patch.object(indexsvc, '_fs_path', lambda prefix, path: root / path), \
         patch('app.storage._fs_path', lambda prefix, path: root / path), \
         patch('app.storage._use_s3', lambda: False), \
         patch.object(indexsvc, '_use_s3', lambda: False):
        indexsvc.rebuild(1, 'case')
        event_id = str(uuid.uuid4())
        consume = lambda: indexsvc.consume_event(1, 'case', event_id, 'files.changed', {'rescan': True})
        (root / 'input.txt').write_text('newword', encoding='utf8')
        original = indexsvc._rebuild_locked
        def fail(conn, prefix, building, **options):
            original(conn, prefix, building, **options)
            raise OSError('Injected failure after projection writes, before receipt/commit')
        with patch.object(indexsvc, '_rebuild_locked', fail):
            try:
                consume()
                raise AssertionError('Fault did not propagate')
            except OSError:
                pass
        assert indexsvc.search(1, 'oldword')['hits'] and not indexsvc.search(1, 'newword')['hits']
        with sqlite3.connect(indexsvc._db_path(1)) as db:
            assert db.execute('SELECT count(*) FROM consumed_events').fetchone()[0] == 0
        checks.append('projection_and_receipt_roll_back_together')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result() for f in [pool.submit(consume), pool.submit(consume)]]
        assert sorted(results) == ['already_consumed', 'consumed'], results
        assert indexsvc.search(1, 'newword')['hits']
        checks.append('concurrent_consumers_apply_once')
        (root / 'input.txt').write_text('latestword', encoding='utf8')
        indexsvc.consume_event(1, 'case', str(uuid.uuid4()), 'files.changed', {'rescan': True})
        building = indexsvc.status(1)['building_uuid']
        assert consume() == 'already_consumed' and indexsvc.status(1)['building_uuid'] == building
        assert indexsvc.search(1, 'latestword')['hits'] and not indexsvc.search(1, 'newword')['hits']
        checks.append('late_duplicate_cannot_replace_newer_index')
        try:
            indexsvc.consume_event(1, 'case', event_id, 'files.changed', {'rescan': False})
            raise AssertionError('Changed event identity accepted')
        except ValueError:
            pass
        assert indexsvc.status(1)['building_uuid'] == building
        checks.append('changed_event_payload_rejected_without_side_effect')
        assert indexsvc.consume_event(2, 'case', str(uuid.uuid4()), 'files.changed', {}) == 'index_not_enabled'
        assert not (indexsvc.INDEX_DIR / 'case-2.sqlite').exists()
        checks.append('disabled_index_not_created_by_delivery')
    return {'passed': True, 'count': len(checks), 'checks': checks}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix='event-consumer-evidence-', dir=SERVER.parents[1] / 'tmp'))
    hashes = {p.relative_to(SERVER).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (SERVER / 'app').rglob('*.py')}
    (report / 'source.json').write_text(json.dumps(hashes, indent=2), encoding='utf8')
    results = {'index_transactions': index_transactions(report), 'sqlite': await asyncio.to_thread(verify)}
    (report / 'sqlite-result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert url.drivername == 'postgresql+asyncpg' and url.host in {'127.0.0.1', 'localhost', '::1'}
        name = 'tmp_vf_consumer_' + uuid.uuid4().hex
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                      password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            target = url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, target, report, 'upgrade', 'head')
            results['postgresql'] = await asyncio.to_thread(verify, target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    assert hashes == {p.relative_to(SERVER).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (SERVER / 'app').rglob('*.py')}
    (report / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('EVENT_CONSUMER_OK ' + str(report), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
