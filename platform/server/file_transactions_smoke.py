"""Real API death around local file writes and SQL commit; fresh-process recovery."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import time
import uuid

import httpx

from testsupport import IsolatedServer, SERVER


FIXTURE = '''
import asyncio, json, time
from pathlib import Path
from fastapi import Depends
from app.services import file_transactions as journal, local_file_edits
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.case_write import coordinate
from app.main import app
root = Path(__file__).parent
def mode():
    return (root/'mode').read_text() if (root/'mode').exists() else ''
def hold():
    (root/'ready').touch()
    deadline = time.monotonic()+90
    while not (root/'release').exists():
        if time.monotonic()>deadline: raise TimeoutError('file journal fixture')
        time.sleep(.02)
original_move, original_apply, original_commit = journal.move_new, journal._apply, AsyncSession.commit
install_file = local_file_edits.PreparedEdit.install_file
finish_local = journal._finish_local
def moving(source, target, **kwargs):
    if target.name=='pending' and mode()=='before_journal': hold()
    return original_move(source, target, **kwargs)
def applying(case, directory, index, entry, side, expected):
    if side=='after' and index==0 and mode()=='before_apply': hold()
    result = original_apply(case, directory, index, entry, side, expected)
    if side=='after' and index==0:
        if mode() in {'after_apply','external','corrupt','batch'}: hold()
        if mode()=='rollback': raise ValueError('fixture rollback after writing')
    if side=='before' and index==0 and mode()=='recovery_crash': hold()
    return result
def installing(edit, case, target, index, entry):
    if index==0 and mode()=='before_apply': hold()
    result = install_file(edit, case, target, index, entry)
    if index==0:
        if mode() in {'after_apply','external','corrupt'}: hold()
        if mode()=='rollback': raise ValueError('fixture rollback after writing')
    return result
def finishing(*args):
    if mode()=='recovery_crash': hold()
    return finish_local(*args)
async def committing(self):
    owned = self.info.get('local_file_transactions', {})
    if owned and mode()=='before_commit': hold()
    result = await original_commit(self)
    if owned and mode() in {'after_commit','committed_external'}: hold()
    return result
journal.move_new, journal._apply, AsyncSession.commit = moving, applying, committing
local_file_edits.PreparedEdit.install_file = installing
journal._finish_local = finishing
@app.post('/fixture/batch/{case_id}')
async def batch(case_id: int, changes: dict, db=Depends(get_db), user=Depends(get_current_user)):
    case = await get_owned_case(case_id, user, db)
    async with coordinate(db, case):
        await journal.write(db, case, {path: value.encode() if value is not None else None for path,value in changes.items()})
    await db.commit()
    await journal.finish_committed(db, case)
    return {'ok': True}
@app.post('/fixture/own-marker/{case_id}')
async def own_marker(case_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    from app import storage
    case = await get_owned_case(case_id, user, db)
    async with coordinate(db, case):
        await journal.write(db, case, {'fault.txt': b'uncommitted'})
        async with coordinate(db, case, content_write=False):
            assert storage.get_bytes(case.s3_prefix, 'fault.txt') == b'uncommitted'
            assert (journal._base(case)/'pending').exists()
    await db.rollback()
    # Reload after rollback; SQLAlchemy expires previously loaded ORM rows.
    await db.refresh(user)
    case = await get_owned_case(case_id, user, db)
    async with coordinate(db, case):
        assert storage.get_bytes(case.s3_prefix, 'fault.txt') == b'old'
        assert not (journal._base(case)/'pending').exists()
    await db.commit()
    return {'ok': True}
'''


def wait_ready(api):
    deadline = time.monotonic() + 35
    while not (api.root/'ready').exists():
        if api.proc.poll() is not None or time.monotonic() > deadline:
            raise AssertionError('Fixture did not reach its crash boundary: ' + str(api.root))
        time.sleep(.03)


def peer(api, *, recovery_crash=False):
    server = IsolatedServer('file-transaction-peer', app='journal_peer:app', database_url=api.database_url)
    overrides = {key: api.env[key] for key in ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_STORAGE_FS_ROOT',
                                              'VIBEFLOW_JWT_SECRET', 'VIBEFLOW_INDEX_ROOT')}
    script = 'import os\nos.environ.update(' + repr(overrides) + ')\n'
    script += FIXTURE if recovery_crash else 'from app.main import app\n'
    (server.root/'journal_peer.py').write_text(script, encoding='utf8')
    if recovery_crash:
        (server.root/'mode').write_text('recovery_crash')
    server.token = api.token
    return server


def interrupt(api, request):
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request)
        wait_ready(api)
        api.proc.terminate()
        api.proc.wait(timeout=10)
        try:
            future.result(timeout=10)
        except (httpx.TransportError, OSError):
            pass
        else:
            raise AssertionError('Terminated request unexpectedly returned success')


def preparation_pages(api, prefix, expected):
    """Exercise the public paging contract and ensure it grants no new access."""
    from urllib.parse import urlencode
    rows, after = [], ''
    while True:
        query = urlencode({'include_preparations':'true','preparation_limit':1,'preparation_after':after})
        value = api.call('GET', prefix+'/file-transactions?'+query)['preparations']
        assert value['read_only'] and len(value['entries'])==1, value
        rows.extend(value['entries'])
        assert len(rows) <= len(expected), 'Preparation pagination did not advance'
        if not value['has_more']:
            assert value['next_cursor'] is None
            break
        assert value['next_cursor'] > after
        after = value['next_cursor']
    assert {row['path'] for row in rows}==set(expected) and len(rows)==len(expected), rows
    assert all(row['commit_state']=='not_determined' and row['decision']=='retain' for row in rows)
    api.call('GET', prefix+'/file-transactions?include_preparations=true&preparation_limit=201', expect=422)
    with httpx.Client(base_url=api.base, trust_env=False) as anonymous:
        assert anonymous.get('/api/v1'+prefix+'/file-transactions?include_preparations=true').status_code in {401,403}
    return rows


def run(database_url=None, modes=None):
    checks, evidence = [], []
    modes = modes or ['before_journal', 'before_apply', 'after_apply', 'before_commit', 'after_commit',
                     'create', 'delete', 'external', 'corrupt', 'committed_external', 'batch', 'recovery_crash', 'rollback', 'normal']
    for mode in modes:
        api = IsolatedServer('file-transaction-' + mode, app='journal_fixture:app', database_url=database_url)
        (api.root/'journal_fixture.py').write_text(FIXTURE, encoding='utf8')
        with api:
            case = api.create_case('File transaction ' + mode)
            api.put('fault.txt', 'old')
            api.put('second.txt', 'second-old')
            root = next((api.root/'files/workspaces').glob(f'*/cases/{case["id"]}'))
            journal = root/'.vibeflow/file-transactions'
            assert not (journal/'pending').exists(), 'Successful HTTP write left a pending journal'
            headers = {'Authorization': 'Bearer ' + api.token}
            with httpx.Client(base_url=api.base, headers=headers, trust_env=False, timeout=45) as client:
                endpoint = '/api/v1' + api.prefix
                if mode == 'normal':
                    assert client.get(endpoint+'/file-transactions').json()['status'] == 'idle'
                    own = client.post('/fixture/own-marker/' + str(case['id']))
                    assert own.status_code == 200, own.text
                    uploaded = client.post(endpoint+'/upload?path=binary.bin', files={'file': ('binary.bin', b'\x00\xffbytes')})
                    assert uploaded.status_code == 200, uploaded.text
                    assert client.get(endpoint+'/raw?path=binary.bin').content == b'\x00\xffbytes'
                    assert client.delete(endpoint+'/file?path=binary.bin').status_code == 200
                    assert client.get(endpoint+'/raw?path=binary.bin').status_code == 404
                    metadata = root/'.vibeflow/tree-state.json'
                    metadata.write_text(json.dumps({'schema': 'vibeflow.case-tree-metadata.v1',
                        'materialize_directories': True, 'entries': {'fault.txt': {
                            'kind': 'file', 'mode': 0o751, 'size': 3, 'sha256': hashlib.sha256(b'old').hexdigest()}}}))
                    edited = client.put(endpoint+'/file', json={'path': 'fault.txt', 'content': 'edited',
                        'expected_revision': case['dict_revision'], 'expected_sha256': hashlib.sha256(b'old').hexdigest()})
                    assert edited.status_code == 200, edited.text
                    entry = json.loads(metadata.read_text())['entries']['fault.txt']
                    assert entry == {'kind': 'file', 'mode': 0o751, 'size': 6,
                                     'sha256': hashlib.sha256(b'edited').hexdigest()}
                    assert client.delete(endpoint+'/file?path=fault.txt').status_code == 200
                    assert json.loads(metadata.read_text())['entries'] == {}
                    protected = client.put(endpoint+'/file', json={'path': '.vibeflow/file-transactions/fake', 'content': 'bad'})
                    assert protected.status_code == 409, protected.text
                    with httpx.Client(base_url=api.base, trust_env=False) as stranger:
                        assert stranger.get(endpoint+'/raw?path=fault.txt').status_code in {401, 403}
                    checks.append('normal_save_upload_delete_and_reserved_namespace')
                    evidence.append({'mode': mode, 'api': str(api.root)})
                    continue
                fault_mode = 'after_apply' if mode in {'create', 'delete', 'recovery_crash'} else mode
                (api.root/'mode').write_text(fault_mode)
                if mode in {'batch', 'recovery_crash'}:
                    request = lambda: client.post('/fixture/batch/' + str(case['id']), json={'fault.txt': 'new', 'second.txt': 'second-new'})
                elif mode == 'before_journal':
                    request = lambda: client.post('/fixture/batch/' + str(case['id']), json={'fault.txt': 'new'})
                elif mode == 'delete':
                    request = lambda: client.delete(endpoint+'/file?path=fault.txt')
                else:
                    request = lambda: client.put(endpoint+'/file', json={'path': 'new.txt' if mode=='create' else 'fault.txt', 'content': 'new'})
                if mode == 'rollback':
                    response = request()
                    assert response.status_code == 422, response.text
                    assert (root/'fault.txt').read_text() == 'new'
                    (api.root/'mode').write_text('')
                else:
                    interrupt(api, request)
            if mode in {'external', 'committed_external'}:
                (root/'fault.txt').write_text('external edit')
            if mode == 'corrupt':
                (journal/'pending/before/0').write_text('corrupt backup')
            if mode == 'recovery_crash':
                with peer(api, recovery_crash=True) as interrupted:
                    with httpx.Client(base_url=interrupted.base, headers=headers, trust_env=False, timeout=45) as reader:
                        interrupt(interrupted, lambda: reader.get('/api/v1'+api.prefix+'/raw?path=fault.txt'))
                    assert (root/'fault.txt').read_text() == 'old'
            with peer(api) as recovered:
                diagnosis = recovered.call('GET', api.prefix+'/file-transactions')
                assert diagnosis['status'] == ('idle' if mode=='before_journal' else 'journal_invalid' if mode=='corrupt'
                                               else 'conflict' if mode in {'external','committed_external'} else 'pending'), diagnosis
                if mode != 'corrupt' and mode != 'before_journal':
                    assert diagnosis['database_committed'] == (mode in {'after_commit','committed_external'})
                path = 'new.txt' if mode == 'create' else 'fault.txt'
                expected = 409 if mode in {'external', 'corrupt', 'committed_external'} else 404 if mode == 'create' else 200
                response = recovered.call('GET', api.prefix+'/raw?path='+path, expect=expected, raw=expected==200)
                if expected == 409:
                    assert response['detail']['code'] == 'file_recovery_required', response
                    assert (root/'fault.txt').read_text() == ('external edit' if mode != 'corrupt' else 'new')
                    assert (journal/'pending').exists()
                    recovered.call('PUT', api.prefix+'/file', {'path': 'unrelated.txt', 'content': 'blocked'}, expect=409)
                    assert not (root/'unrelated.txt').exists()
                else:
                    if expected == 200:
                        assert response == (b'new' if mode=='after_commit' else b'old'), (mode, response)
                    assert (root/'second.txt').read_text() == 'second-old'
                    assert not (journal/'pending').exists()
                    before = (root/'fault.txt').stat().st_mtime_ns
                    recovered.call('GET', api.prefix+'/tree')
                    assert (root/'fault.txt').stat().st_mtime_ns == before, 'Repeated recovery rewrote retained bytes'
                    if mode != 'before_journal':
                        receipts = [json.loads(p.read_text()) for p in (journal/'history').glob('*/resolution.json')]
                        assert any(item['decision'] == ('committed' if mode=='after_commit' else 'rolled_back') for item in receipts)
                    else:
                        assert list(journal.glob('preparing-*')), 'Unpublished preparation must remain diagnosable'
                        retained = {p.relative_to(journal).as_posix():p.read_bytes()
                                    for folder in journal.glob('preparing-*') for p in folder.rglob('*') if p.is_file()}
                        detail = recovered.call('GET', api.prefix+'/file-transactions?include_preparations=true')
                        inventory = detail['preparations']
                        assert detail['status']=='idle' and inventory['read_only'] and not inventory['has_more'], detail
                        assert len(inventory['entries'])==1, inventory
                        row = inventory['entries'][0]
                        assert row['state']=='preparation_retained' and row['commit_state']=='not_determined', row
                        assert row['decision']=='retain' and row['transaction_id'], row
                        assert (root/row['path']).is_dir()
                        assert retained == {p.relative_to(journal).as_posix():p.read_bytes()
                                    for folder in journal.glob('preparing-*') for p in folder.rglob('*') if p.is_file()}
                        assert recovered.call('GET', api.prefix+'/raw?path=fault.txt', raw=True)==b'old'
                        partial = journal/('preparing-'+str(uuid.uuid4()))
                        partial.mkdir(); (partial/'manifest.json').write_bytes(b'{partial')
                        odd = journal/'preparing-legacy'; odd.write_bytes(b'unknown evidence')
                        outside = api.root/'inventory-outside'; outside.mkdir()
                        (outside/'private.txt').write_bytes(b'outside secret must not be read')
                        linked = journal/('preparing-'+str(uuid.uuid4()))
                        linked.symlink_to(outside, target_is_directory=True)
                        expected = [row['path'], *[p.relative_to(root).as_posix() for p in (partial,odd,linked)]]
                        paged = preparation_pages(recovered, api.prefix, expected)
                        assert next(r for r in paged if r['path']==linked.relative_to(root).as_posix())['kind']=='link'
                        assert next(r for r in paged if r['path']==odd.relative_to(root).as_posix())['state']=='unverified_retained'
                        assert 'outside secret' not in json.dumps(paged)
                        assert (outside/'private.txt').read_bytes()==b'outside secret must not be read'
                        assert (partial/'manifest.json').read_bytes()==b'{partial' and odd.read_bytes()==b'unknown evidence'
            checks.append(mode)
            evidence.append({'mode': mode, 'api': str(api.root), 'peer': str(recovered.root)})
        print('PASS ' + mode, flush=True)
    return {'checks': checks, 'count': len(checks), 'runs': evidence}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--modes', nargs='+')
    parser.add_argument('--postgres-only', action='store_true')
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix='file-transactions-evidence-', dir=SERVER.parent.parent/'tmp'))
    if args.postgres_only:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        pg = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert pg.drivername == 'postgresql+asyncpg' and pg.host in {'127.0.0.1', 'localhost', '::1'}
        name = 'tmp_vf_filejournal_' + uuid.uuid4().hex
        admin = await asyncpg.connect(host=pg.host, port=pg.port or 5432, user=pg.username,
                                      password=pg.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            url = pg.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, url, output, 'upgrade', 'head')
            result = await asyncio.to_thread(run, url, args.modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        result.update(database='postgresql', temporary_database_removed=True)
    else:
        result = await asyncio.to_thread(run, modes=args.modes)
        result['database'] = 'sqlite'
    (output/'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps({'status': 'passed', 'count': result['count'], 'evidence': str(output)}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
