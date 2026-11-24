"""Exercise selected-file restore against an actual retired WSL shell and versioned S3."""

import argparse
import asyncio
import json
from pathlib import Path
import uuid

import aiohttp

from s3testsupport import ObjectStore
from sandbox_normal_close_resource_smoke import RUNTIME, call, case_prefix, inspect_source, wait_file, writer_available
from sandbox_reserved_tree_acceptance import DIGEST, FIXTURE, SIZE, TARGET, arm, other_case, rows, while_transfer
from sandbox_retirement_checks import verify as verify_retirement

DICT = '0-caseDict/caseDict'
HISTORY = ('0-caseDict/case-events.jsonl', '0-caseDict/provenance.jsonl')
CREATE = 'Ref/restore-create.txt'
DELETE = 'Ref/restore-delete.txt'
NOTE = 'selected dictionary restore from WSL'

OBSERVATIONS = r'''
from app.services import sandbox_recovery_restore
from app.services.case_write import CaseWriteConflict

selected_preview = ContextVar('selected_restore_preview', default=False)
select_files = sandbox_recovery_restore._selected_s3_files
get_selected = s3_live_store.Client.get_object
apply_selected = s3_file_transactions._apply
restore_deadline = sandbox_recovery_restore.file_read_deadline


def observe_selection(*args):
    token = selected_preview.set((base/'observe-preview').exists())
    try:
        return select_files(*args)
    finally:
        selected_preview.reset(token)


class PreviewBody(Observed):
    def read(self, amount=None):
        data = self.body.read(amount)
        if data and not self.reported:
            self.reported = True
            marker = base/'preview-observed.json'
            pending = marker.with_suffix('.tmp')
            pending.write_text(json.dumps({'first_read': len(data), 'length': self.content_length,
                'inside_sql': storage_operations.inside_control() or _writes.get() is not None}), encoding='utf8')
            pending.replace(marker)
            end = time.monotonic() + 20
            while not (base/'release-preview').exists():
                if time.monotonic() >= end:
                    raise RuntimeError('Fixture did not release preview')
                time.sleep(.025)
        return data

    def close(self):
        self.body.close()


def observe_preview(self, **args):
    response = get_selected(self, **args)
    if selected_preview.get() and args['Key'].endswith('/Ref/remote-publication.bin'):
        response['Body'] = PreviewBody(response['Body'], response['ContentLength'])
    return response


def fail_after_write(case, directory, index, entry, side, expected_side, **options):
    result = apply_selected(case, directory, index, entry, side, expected_side, **options)
    flag = base/'fail-dictionary'
    if side == 'after' and entry['path'] == '0-caseDict/provenance.jsonl' and flag.exists():
        flag.unlink()
        raise CaseWriteConflict('fixture_dictionary_after_write', 'Fault after writing actual S3 provenance')
    flag = base/'fail-batch'
    if side == 'after' and entry['path'] == 'Ref/restore-create.txt' and flag.exists():
        flag.unlink()
        raise CaseWriteConflict('fixture_batch_after_write', 'Fault after writing an actual restored file')
    return result


def observed_deadline(code, label, **options):
    if code == 'sandbox_file_restore' and (base/'short-file-restore').exists():
        options['seconds'] = 24
    return restore_deadline(code, label, **options)


sandbox_recovery_restore._selected_s3_files = observe_selection
sandbox_recovery_restore.file_read_deadline = observed_deadline
s3_live_store.Client.get_object = observe_preview
s3_file_transactions._apply = fail_after_write
'''


async def confirmation(api, base, semantics, paths):
    selection = {'paths': paths, 'semantics': semantics}
    plan = await call(api, 'POST', base+'/restore-plan', selection)
    return {**selection, 'confirm': True, 'plan_sha256': plan['plan_sha256'],
            'request_id': str(uuid.uuid4())}


async def check_timeout(api, base, other, body):
    flag = api.root/'short-file-restore'
    flag.touch()
    arm(api)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100)) as client:
            async def restore():
                async with client.post(api.base+'/api/v1'+base+'/restore', json=body,
                                       headers={'Authorization': 'Bearer '+api.token}) as response:
                    return response.status, await response.json()

            pending = asyncio.create_task(restore())
            try:
                evidence = await while_transfer(api, other, request=pending)
                await asyncio.sleep(evidence['deadline_remaining'] + .1)
                assert not (api.root/'transfer-drained').exists()
            finally:
                (api.root/'release-copy').touch()
            status, error = await pending
    finally:
        flag.unlink()
    assert status == 504 and error['detail']['code'] == 'sandbox_file_restore_timeout', (status, error)
    assert (api.root/'transfer-drained').exists()
    assert await call(api, 'GET', api.prefix+'/raw?path='+TARGET, raw=True) == b'changed during preview'
    await call(api, 'GET', base+'/restores/'+body['request_id'], expect=404)
    assert not rows(api, 'SELECT id FROM case_storage_operations WHERE case_id=? AND state=?', api.case_id, 'active')
    return {**evidence, 'status': 504, 'preserved_original': True, 'transfer_drained': True, 'receipt': 404}


async def check_files(api, base, store, report):
    flag = api.root/'observe-preview'
    flag.touch()
    preview = asyncio.create_task(call(api, 'POST', base+'/restore-plan', {'paths': [TARGET]}, expect=409))
    try:
        observed = await wait_file(api.root/'preview-observed.json', 15)
        assert 0 < observed['first_read'] <= 1024*1024 and observed['length'] == SIZE, observed
        assert not observed['inside_sql'] and await asyncio.to_thread(writer_available, api), observed
        await call(api, 'PUT', api.prefix+'/file', {'path': TARGET, 'content': 'changed during preview'})
    finally:
        flag.unlink()
        (api.root/'release-preview').touch()
    conflict = await preview
    assert conflict['detail']['code'] == 'recovery_plan_stale', conflict
    report['preview'] = {**observed, 'same_case_save': 200, 'stale_preview': 409}

    other = await other_case(api, 'Other case during S3 file restore')
    body = await confirmation(api, base, 'file_contents_only', [TARGET])
    report['timeout'] = await check_timeout(api, base, other, body)
    arm(api)
    pending = asyncio.create_task(call(api, 'POST', base+'/restore', body))
    try:
        observed = await while_transfer(api, other, request=pending)
    finally:
        (api.root/'release-copy').touch()
    receipt = await pending
    assert receipt['file_transaction_id'] and receipt['written_files'] == [TARGET] and not receipt['replayed'], receipt
    stored = await asyncio.to_thread(inspect_source, store, case_prefix(api)+'/'+TARGET)
    assert stored['sha256'] == DIGEST and stored['size'] == SIZE, stored
    await call(api, 'PUT', api.prefix+'/file', {'path': TARGET, 'content': 'edited after restore'})
    replay = await call(api, 'POST', base+'/restore', body)
    assert replay == {**receipt, 'replayed': True}, replay
    assert await call(api, 'GET', api.prefix+'/raw?path='+TARGET, raw=True) == b'edited after restore'
    report['selected_file'] = {**observed, 'sha256': stored['sha256'], 'receipt': receipt,
                               'retried_after_timeout': True, 'replayed': True}

    batch = await confirmation(api, base, 'sandbox_file_changes', [CREATE, DELETE])
    (api.root/'fail-batch').touch()
    failure = await call(api, 'POST', base+'/restore', batch, expect=409)
    assert failure['detail']['code'] == 'fixture_batch_after_write', failure
    await call(api, 'GET', api.prefix+'/raw?path='+CREATE, expect=404)
    assert await call(api, 'GET', api.prefix+'/raw?path='+DELETE, raw=True) == b'delete from WSL'
    await call(api, 'GET', base+'/restores/'+batch['request_id'], expect=404)
    receipt = await call(api, 'POST', base+'/restore', batch)
    assert receipt['deleted_files'] == [DELETE] and receipt['deletions_applied'] and receipt['file_transaction_id'], receipt
    await call(api, 'GET', api.prefix+'/raw?path='+DELETE, expect=404)
    assert await call(api, 'GET', api.prefix+'/raw?path='+CREATE, raw=True) == b'created in WSL'
    report['batch'] = {**receipt, 'partial_write_rolled_back': True, 'same_request_retried': True}


async def check_dictionary(api, base, report):
    async def read_sources():
        return [await call(api, 'GET', api.prefix+'/raw?path='+path, raw=True) for path in (DICT, *HISTORY)]

    endpoint = api.prefix+'/caseDict'
    original = await call(api, 'GET', endpoint)
    stale = await confirmation(api, base, 'case_dictionary', [DICT])
    await call(api, 'PUT', endpoint, {'content': original['content'], 'expected_revision': original['dict_revision']})
    current = await call(api, 'GET', endpoint)
    assert current['dict_revision'] == original['dict_revision']+1
    conflict = await call(api, 'POST', base+'/restore', stale, expect=409)
    assert conflict['detail']['code'] == 'recovery_plan_stale', conflict
    before = await read_sources()

    body = await confirmation(api, base, 'case_dictionary', [DICT])
    (api.root/'fail-dictionary').touch()
    fault = await call(api, 'POST', base+'/restore', body, expect=409)
    assert fault['detail']['code'] == 'fixture_dictionary_after_write', fault
    assert await read_sources() == before
    unchanged = await call(api, 'GET', endpoint)
    assert unchanged['dict_revision'] == current['dict_revision']
    await call(api, 'GET', base+'/restores/'+body['request_id'], expect=404)

    receipt = await call(api, 'POST', base+'/restore', body)
    updated = await call(api, 'GET', endpoint)
    assert NOTE in updated['content'] and updated['dict_revision'] == current['dict_revision']+1
    assert receipt['dictionary_revision'] == updated['dict_revision'] and receipt['file_transaction_id'] is None, receipt
    for old, new in zip(before[1:], (await read_sources())[1:]):
        assert new.startswith(old) and new.count(b'sandbox-recovery') > old.count(b'sandbox-recovery')
    replay = await call(api, 'POST', base+'/restore', body)
    assert replay == {**receipt, 'replayed': True}, replay
    assert (await call(api, 'GET', endpoint))['dict_revision'] == updated['dict_revision']
    report['dictionary'] = {'receipt': receipt, 'stale_revision': 409, 'fault_rolled_back_three_files': True,
                            'fault_preserved_revision': True, 'fault_receipt': 404, 'replayed': True}


async def verify(store, report):
    directory = None

    async def before(api):
        nonlocal directory
        directory = api.root
        report['api_root'] = str(directory)
        await call(api, 'PUT', api.prefix+'/file', {'path': DELETE, 'content': 'delete from WSL'})

    async def after(api, sid, rid, proof):
        base = f'/terminal/sessions/{sid}/recoveries/{rid}'
        await check_files(api, base, store, report)
        await check_dictionary(api, base, report)
        return 0

    async def before_recovery(api, sid, proof):
        # Establish a real large destination after the initial WSL inventory.
        path = api.root/'large-destination.bin'
        with path.open('wb') as stream:
            stream.write(b'X')
            stream.truncate(SIZE)
        try:
            def upload():
                with path.open('rb') as stream:
                    store.client.put_object(Bucket=store.bucket, Key=case_prefix(api)+'/'+TARGET,
                                            Body=stream, IfNoneMatch='*')
            await asyncio.to_thread(upload)
        finally:
            path.unlink()

    try:
        await verify_retirement(store, fixture_prefix=FIXTURE+OBSERVATIONS, before_sandbox=before,
            before_recovery=before_recovery, after_retirement=after,
            sandbox_command=(f': > {TARGET}; truncate -s {SIZE} {TARGET}; rm {DELETE}; '
                             f"printf '%s' 'created in WSL' > {CREATE}; "
                             f"printf '\\n// {NOTE}\\n' >> {DICT}; "))
    finally:
        if directory is not None:
            (directory/'retirement_fixture.py').unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    path = parser.parse_args().report
    report = {'passed': False, 'actual_model_calls': 0, 'mock_llm_calls': 0}
    try:
        with ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) as store:
            asyncio.run(verify(store, report))
        report['passed'] = True
    finally:
        path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print('SANDBOX_S3_FILE_RESTORE_PASS', path, flush=True)
