"""Actual FS/SeaweedFS listing bounds, deadlines and logical catalog compatibility."""
import asyncio
import concurrent.futures
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from testsupport import SERVER
from s3testsupport import ObjectStore


def main():
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    root = Path(tempfile.mkdtemp(prefix='storage-listing-', dir=SERVER.parents[1] / 'tmp'))
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(root / 'files'), VIBEFLOW_STORAGE_BACKEND='fs',
                      VIBEFLOW_DATABASE_URL='sqlite+aiosqlite:///' + (root / 'unused.sqlite').as_posix())
    from app import storage
    from app.config import get_settings
    from app.services.case_write import CaseWriteConflict
    from app.services import request_deadline
    get_settings.cache_clear()
    report = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0, 'checks': []}
    store = None

    @contextmanager
    def limits(entries=1000000, seconds=30):
        keys = {'VIBEFLOW_STORAGE_MEASUREMENT_MAX_ENTRIES': str(entries),
                'VIBEFLOW_STORAGE_MEASUREMENT_TIMEOUT_SECONDS': str(seconds)}
        previous = {key: os.environ.get(key) for key in keys}
        os.environ.update(keys); get_settings.cache_clear()
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:os.environ.pop(key, None)
                else:os.environ[key] = value
            get_settings.cache_clear()

    def denied(call, code):
        try:call()
        except CaseWriteConflict as error:
            assert error.code == code, error.detail()
        else:raise AssertionError('Incomplete listing accepted as complete: ' + code)

    try:
        expected = []
        for index in range(25):
            path = f'{index:02}.bin'; body = bytes([index]) * (index + 1)
            storage._fs_put('case', path, body)
            expected.append((path, len(body)))
        for index in range(3):storage._fs_path('case', f'empty-{index}').mkdir()
        with limits(28):
            assert [(row['path'], row['size']) for row in storage.list_tree('case')] == expected
        with limits(27):denied(lambda: storage.list_tree('case'), 'storage_listing_limit')
        report['checks'].append('FS exact complete inventory at bound; one less refuses without a partial result')
        for index in range(12):storage._fs_path('directories', str(index)).mkdir(parents=True)
        with limits(10):denied(lambda: storage.list_tree('directories'), 'storage_listing_limit')
        report['checks'].append('FS empty directories consume the scan budget')

        original = os.scandir
        observed = {'entries': 0, 'closed': False}
        class SlowDirectory:
            def __init__(self, path):self.real = original(path)
            def __enter__(self):self.real.__enter__(); return self
            def __exit__(self, *args):
                self.real.__exit__(*args); observed['closed'] = True
            def __iter__(self):return self
            def __next__(self):
                item = next(self.real); observed['entries'] += 1; time.sleep(.15); return item
        os.scandir = SlowDirectory
        try:
            with limits(seconds=.1):denied(lambda: storage.list_tree('case'), 'storage_listing_timeout')
            assert observed == {'entries': 1, 'closed': True}, observed
            observed.update(entries=0, closed=False)
            async def expired_parent():
                async with request_deadline.scope(.1):storage.list_tree('case')
            try:asyncio.run(expired_parent())
            except TimeoutError:pass
            else:raise AssertionError('Parent deadline was refreshed')
            assert observed == {'entries': 1, 'closed': True}, observed
        finally:os.scandir = original
        report['checks'].append('FS actual enumeration pause expires local and inherited deadlines; scan handles close')

        store = ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed')
        with store:
            os.environ.update(store.settings()); get_settings.cache_clear(); storage._client = store.client
            raw = store.raw_client; bucket = store.bucket
            def put(index):
                raw.put_object(Bucket=bucket, Key=f'plain/{index:04}.bin', Body=bytes([index % 256]))
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(put, range(1025)))
            pages = []
            def page_received(**event):pages.append(event['parsed']['KeyCount'])
            raw.meta.events.register('after-call.s3.ListObjectsV2', page_received, unique_id='listing-observer')
            with limits(1025):
                result = storage.list_tree('plain')
                assert [r['path'] for r in result] == [f'{i:04}.bin' for i in range(1025)]
            assert pages == [1000, 25], pages
            report['checks'].append('S3 actual 1025 objects use two raw pages; sorted complete inventory at bound')
            pages.clear()
            with limits(1000):denied(lambda: storage.list_tree('plain'), 'storage_listing_limit')
            assert pages == [1000, 25], pages
            report['checks'].append('S3 merged paginator rejects overflow before yielding any logical page')
            pages.clear()
            def delay(**event):time.sleep(.15)
            raw.meta.events.register('after-call.s3.ListObjectsV2', delay, unique_id='listing-delay')
            try:
                with limits(seconds=.1):denied(lambda: storage.list_tree('plain'), 'storage_listing_timeout')
                assert pages == [1000], pages
            finally:raw.meta.events.unregister('after-call.s3.ListObjectsV2', unique_id='listing-delay')
            report['checks'].append('S3 actual first page pause expires deadline; no second HTTP listing')
            raw.meta.events.unregister('after-call.s3.ListObjectsV2', unique_id='listing-observer')
            prefix = 'workspaces/1/cases/1'
            storage._s3_put(prefix, 'Ref/first.txt', b'first')
            storage._s3_put(prefix, 'Ref/second.txt', b'second')
            storage._s3_delete(prefix, 'Ref/first.txt')
            storage._s3_put(prefix, '.vibeflow/attempts/probe/private.bin', b'private')
            rows = storage.list_tree(prefix, 'Ref')
            assert [r['path'] for r in rows] == ['Ref/second.txt'], rows
            assert storage.get_bytes(prefix, rows[0]['path']) == b'second'
            assert [r['path'] for r in storage._s3_list(prefix, 'Ref')] == ['second.txt']
            all_rows = storage.list_tree(prefix)
            assert {r['path'] for r in all_rows} == {'Ref/second.txt', '.vibeflow/attempts/probe/private.bin'}
            report['checks'].append('Actual logical CAS/tombstone, private inventory, subdirectory paths and bytes remain compatible')
            with limits(1):
                denied(lambda: list(store.client.get_paginator('list_objects_v2').paginate(
                    Bucket=bucket, Prefix=prefix + '/Ref/', PaginationConfig={'PageSize': 1})), 'storage_listing_limit')
            report['checks'].append('Logical catalog entries including tombstones count even when raw prefix has no objects')
            assert [r['path'] for r in storage.list_tree(prefix, 'Ref')] == ['Ref/second.txt']
            report['checks'].append('A fresh complete listing succeeds after a refused scan; source remains unchanged')
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:2000]}
    finally:
        if store is not None:
            report['object_store_root'] = str(store.root)
            report['object_store_stopped'] = store.proc is not None and store.proc.poll() is not None
            report['passed'] = report['passed'] and report['object_store_stopped']
        (root / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print('LISTING_RESULT ' + str(root / 'acceptance.json'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':raise SystemExit(main())
