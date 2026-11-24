"""Public CLI contract capture, apply and restore with actual storage and model readback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import time

from live_llm_case_provenance_capacity_acceptance import sql_available, wait_for
from live_llm_job_repeat_acceptance import main
from s3testsupport import ObjectStore, SERVER


FIXTURE = '''
from contextlib import contextmanager
import hashlib
import time
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import cli_contracts
from app.services.file_read import FileRead

original_contract_open = FileRead._open

class PausedContract:
    def __init__(self, stream, marker):
        self.stream = stream
        self.marker = marker

    def read(self, size):
        block = self.stream.read(size)
        entered = root/(self.marker+'-entered')
        if block and not entered.exists():
            entered.write_text(str(len(block)), encoding='utf-8')
            stop = time.monotonic()+23
            while not (root/(self.marker+'-release')).exists():
                if time.monotonic() >= stop:
                    raise TimeoutError('CLI contract read was not released')
                time.sleep(.025)
        return block

@contextmanager
def open_contract(self, *args, **kwargs):
    path = self.location['Key'] if self.remote else self.path.as_posix()
    with original_contract_open(self, *args, **kwargs) as (stream, size):
        marker = root/'contract-pause'
        if path.endswith('/'+cli_contracts.STATE) and marker.exists():
            stream = PausedContract(stream, marker.read_text(encoding='utf-8'))
        yield stream, size

FileRead._open = open_contract

@app.post('/api/v1/testing/cli-contracts/{case_id}/seed')
async def seed_contract(case_id: int, user=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    snapshots = []
    original = 'original instructions\\r\\n'
    for index in range(32):
        content = ('Old instruction text '+str(index)+'\\r\\n') * 1200
        snapshots.append(cli_contracts.Snapshot(id=f'{index:032x}', created_at='2026-01-01', created_by=user.id,
            files={'AGENTS.md':content, 'CLAUDE.md':content}))
    state = cli_contracts.State(backups=snapshots).model_dump_json().encode('utf-8')
    storage.put_bytes(case.s3_prefix, cli_contracts.STATE, state)
    storage.put_text(case.s3_prefix, 'AGENTS.md', original)
    return {'bytes':len(state), 'sha256':hashlib.sha256(state).hexdigest()}

@app.post('/api/v1/testing/cli-contracts/deadline/{seconds}')
async def set_contract_deadline(seconds: int, user=Depends(get_current_user)):
    get_settings().storage_measurement_timeout_seconds = seconds
    return {'seconds':seconds}
'''


def scenario(api, *, store=None, **_):
    route = api.prefix+'/cli-contract'
    seeded = api.call('POST', f'/testing/cli-contracts/{api.case_id}/seed')
    assert seeded['bytes'] > 1024*1024
    status = api.call('GET', route)
    assert len(status['backups']) == 32 and status['selected'] is None
    workspace = api.call('GET', api.prefix)['workspace_id']
    other = api.call('POST', f'/workspaces/{workspace}/cases', {'name':'Independent CLI contract write'})
    other_file = f"/cases/{other['id']}/file"
    body = {'harness':'codex', 'instructions':'Maintain the case inputs.', 'expected_revision':status['revision']}

    def pause(pool, name, method, value=None, *, expect=200):
        marker = api.root/'contract-pause'
        marker.write_text(name, encoding='utf-8')
        future = pool.submit(api.call, method, route, value, expect=expect)
        entered = api.root/(name+'-entered')
        try:
            wait_for(future, entered)
            assert int(entered.read_text(encoding='utf-8')) == 64*1024
            assert sql_available(api), 'Real contract body read holds a SQL write lock'
            if name == 'deadline':
                # The paused request already captured its immutable four-second
                # deadline. Give the independent request its normal budget.
                api.call('POST', '/testing/cli-contracts/deadline/60')
            saved = api.call('PUT', other_file, {'path':f'Ref/{name}.txt','content':'saved during read'})
            assert saved['ok'] and not future.done()
            if name == 'read':
                assert api.put('AGENTS.md', 'newer instructions\r\n')['ok']
            if name == 'deadline':
                time.sleep(6)
                assert not future.done(), 'Read admission released before the paused read drained'
        finally:
            (api.root/(name+'-release')).touch()
        result = future.result(timeout=30)
        marker.unlink()
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        conflict = pause(pool, 'read', 'GET', expect=409)
        assert conflict['detail']
        status = api.call('GET', route)
        body['expected_revision'] = status['revision']
        applied = pause(pool, 'apply', 'POST', body)
        assert applied['ok']
        assert api.call('POST', route, body, expect=409)['detail']
        current = api.call('GET', route)
        assert current['selected'] == 'codex' and current['applied'] and len(current['backups']) == 32
        restore = route+f"/backups/{applied['backup_id']}:restore"
        assert api.call('POST', restore, {'expected_revision':current['revision']})['ok']
        content = api.call('GET', api.prefix+'/raw?path=AGENTS.md', raw=True)
        assert content == b'newer instructions\r\n'
        restored = api.call('GET', route)
        assert restored['selected'] is None
        body['expected_revision'] = restored['revision']

        api.call('POST', '/testing/cli-contracts/deadline/4')
        try:
            timed_out = pause(pool, 'deadline', 'POST', body, expect=504)
        finally:
            api.call('POST', '/testing/cli-contracts/deadline/60')
        assert timed_out['detail']['code'] == 'cli_contract_timeout'
        assert api.call('GET', route)['revision'] == restored['revision']
        assert api.call('POST', route, body)['ok']

    evidence = {'passed':True,'backend':'s3' if store else 'fs','seed':seeded,'actual_chunk_bytes':65536,
        'lock_free_reads':3,'other_case_saves':3,'rejected_changed_read':True,'rejected_stale_apply':True,
        'backup_restored_byte_exactly':True,'timed_out_without_commit':True,'same_revision_retry':True}
    (api.root/'cli-contract-capacity.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    return evidence


if __name__ == '__main__':
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE,
        store_factory=partial(ObjectStore, executable=binary, volume_max=6, authenticated=True)))
