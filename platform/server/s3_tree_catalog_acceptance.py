"""Measure actual S3 catalog requests and verify pinned directory tree versions."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from app import storage
from app.config import get_settings
from app.services import case_tree, tree_metadata_native as metadata, tree_transactions_s3 as trees
from app.services.case_write import CaseWriteConflict
from app.services.s3_live_store import DIRECTORY, HEAD
from s3testsupport import ObjectStore
from testsupport import SERVER


COUNT = 256
LARGE = 'Ref/large.bin'
EMPTY = 'Ref/empty.bin'
LOG = '.vibeflow/jobs/job-7.log'
PREFIX = 'workspaces/1/cases/701'


def seed(client, bucket):
    data = {f'Ref/files/{index:04}.txt': f'member {index}'.encode() for index in range(COUNT)}
    data.update({LARGE: bytes(range(256)) * (12*1024*1024//256), EMPTY: b'', LOG: b'private log'})
    state = {'schema': metadata.SCHEMA, 'materialize_directories': True, 'entries': {
        'Ref': {'kind': 'directory', 'mode': 0o750},
        'Ref/empty-directory': {'kind': 'directory', 'mode': 0o700},
        LARGE: {'kind': 'file', 'mode': 0o640, 'size': len(data[LARGE]),
                'sha256': hashlib.sha256(data[LARGE]).hexdigest()}}}
    data[metadata.PATH] = json.dumps(state).encode()
    snapshot = client.snapshot(bucket, PREFIX)
    written = client.commit_snapshot(snapshot, {name: lambda body=body: body for name, body in data.items()},
                                     watch_paths=data)
    return data, written


def measure(raw, operation):
    original = raw._make_api_call
    calls = Counter()
    transfers = Counter()

    def observe(name, args):
        response = original(name, args)
        key = args.get('Key', '')
        calls[name] += 1
        if name == 'GetObject' and key == PREFIX+'/'+HEAD:
            calls['catalog_reads'] += 1
            transfers['catalog_bytes'] += response['ContentLength']
        if name == 'GetObject' and key.startswith(PREFIX+'/'+DIRECTORY+'/objects/'):
            calls['body_reads'] += 1
            transfers['body_bytes'] += response['ContentLength']
            assert args.get('IfMatch'), key
        return response

    started = time.monotonic()
    with patch.object(raw, '_make_api_call', new=observe):
        result = operation()
    return result, dict(calls, **transfers, elapsed=round(time.monotonic()-started, 3))


def stale(operation):
    try:
        operation()
    except CaseWriteConflict as error:
        assert error.code == 'recovery_plan_stale', error
    else:
        raise AssertionError('A changed tree was accepted')


def verify(client, raw, bucket):
    data, catalog = seed(client, bucket)
    (legacy_scan, legacy_state), repeated = measure(raw, lambda: case_tree.current(PREFIX))
    prepared, pinned = measure(raw, lambda: trees.prepare_current(PREFIX))
    assert prepared.entries == legacy_scan and prepared.metadata == legacy_state
    assert prepared.entries[LARGE]['mode'] == 0o640
    assert prepared.entries['Ref/empty-directory']['mode'] == 0o700
    assert prepared.entries[EMPTY]['sha256'] == hashlib.sha256(b'').hexdigest()
    assert LOG not in prepared.entries
    assert repeated['catalog_reads'] >= 2*COUNT and pinned['catalog_reads'] == 2
    assert pinned['body_reads'] == COUNT+3 and pinned.get('HeadObject', 0) == 0
    assert repeated['body_bytes'] == pinned['body_bytes']

    # Exercise an unrelated HEAD update and an out-of-band physical overwrite.
    client.put_object(Bucket=bucket, Key=PREFIX+'/'+LOG, Body=b'new private log')
    _, verified = measure(raw, lambda: trees._require_current(PREFIX, prepared))
    assert verified['catalog_reads'] == 2 and verified['HeadObject'] == COUNT+3
    assert not verified.get('body_reads')
    entry = catalog.value['files'][LARGE]
    raw.put_object(Bucket=bucket, Key=entry['key'], Body=b'z'*len(data[LARGE]), IfMatch=entry['physical_etag'])
    stale(lambda: trees._require_current(PREFIX, prepared))
    raw.put_object(Bucket=bucket, Key=entry['key'], Body=data[LARGE])
    trees._require_current(PREFIX, prepared)
    client.put_object(Bucket=bucket, Key=PREFIX+'/Ref/files/0000.txt', Body=b'logical replacement')
    stale(lambda: trees._require_current(PREFIX, prepared))

    # A legacy namespace remains readable before any writer migrates it.
    old_prefix = 'workspaces/1/cases/702'
    raw.put_object(Bucket=bucket, Key=old_prefix+'/Ref/old.txt', Body=b'legacy')
    assert trees.prepare_current(old_prefix) is None
    old, raw_state = case_tree.current(old_prefix)
    assert raw_state is None and old['Ref/old.txt']['sha256'] == hashlib.sha256(b'legacy').hexdigest()
    return {'ordinary_files': COUNT+2, 'repeated': repeated, 'pinned': pinned, 'version_recheck': verified,
            'changed_physical_version_rejected': True, 'changed_logical_version_rejected': True,
            'unrelated_private_version_accepted': True, 'legacy_readable': True}


def run():
    runtime = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    with ObjectStore(executable=runtime, volume_max=10, authenticated=True) as store:
        with patch.dict(os.environ, store.settings()), patch.object(storage, '_client', None):
            get_settings.cache_clear()
            try:
                with closing(storage.s3()) as client:
                    report = verify(client, client.raw.raw, store.bucket)
            finally:
                get_settings.cache_clear()
    assert store.proc.poll() is not None
    report['store'] = str(store.root/'store.json')
    return report


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    enforce([Path(__file__).resolve()])
    result = dict(run(), passed=True, actual_model_calls=0, simulated_model_calls=0)
    args.report.write_text(json.dumps(result, indent=2)+'\n', encoding='utf8')
    print('S3_TREE_CATALOG_PASS', args.report, flush=True)
