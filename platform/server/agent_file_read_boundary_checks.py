"""Actual local file boundary checks, with no model/provider/CLI inference.

These check tool implementation limits only; live_llm_read_tools_acceptance.py
separately verifies model selection, tool results and actual model continuation.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

from testsupport import IsolatedServer, SERVER


FIXTURE = '''
import asyncio
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path
from threading import Event
import time
from fastapi import Depends
from app import storage
from app.db import SessionLocal
from app.main import app
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.agent_file_reads import stop_signal
from app.services.agent_storage_capacity import read_case
from app.services.file_read import FileRead
from app.services.tools import ToolExecutor

plan = ContextVar('file_read_test_plan', default=None)
controls = {}
original = FileRead.chunks

def chunks(self, *args):
    selected = plan.get()
    with closing(original(self, *args)) as stream:
        for block in stream:
            if selected is not None and stop_signal.get() is not None:
                selected['blocks'] += 1
                if selected['blocks'] == 1 and selected['hold']:
                    selected['ready'].set()
                    deadline = time.monotonic() + 18
                    while not selected['release'].wait(.03):
                        if selected['user_revocation'] and (Path(__file__).parent / 'release-revoked').is_file():
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Test did not release the actual read')
            yield block

FileRead.chunks = chunks

if storage._use_s3():
    raw = storage.s3().raw
    def record(model, params, **kwargs):
        selected = plan.get()
        if selected is not None and (stop_signal.get() is not None or selected['legacy']):
            selected['requests'].append([model.name, params.get('Key', params.get('Prefix')), params.get('MaxKeys')])
    for name in ('GetObject', 'HeadObject', 'ListObjectsV2'):
        raw.meta.events.register('before-parameter-build.s3.' + name, record)

@app.post('/fixture/read/{case_id}/{key}')
async def run_tool(case_id: int, key: str, body: dict, user=Depends(get_current_user)):
    async with SessionLocal() as db:
        case = await get_owned_case(case_id, user, db)
        prefix = case.s3_prefix
    selected = controls[key] = dict(hold=body.pop('hold', False), ready=Event(), release=Event(),
                                     cancel=asyncio.Event(), blocks=0, requests=[], legacy=body.pop('legacy', False),
                                     user_revocation=key == 'revoked')
    token = plan.set(selected)
    try:
        if selected['legacy']:
            executor = ToolExecutor('workspaces/1000000/cases/1000000')
            result = await asyncio.to_thread(executor.execute, body['tool'], body['args'])
        else:
            executor = ToolExecutor(prefix, case_id=case_id, user_id=user.id)
            result = await read_case(case_id, prefix, user.id, executor.execute,
                                     body['tool'], body['args'], cancel=selected['cancel'])
        return dict(result=result, blocks=selected['blocks'], requests=selected['requests'])
    finally:
        plan.reset(token)

@app.post('/fixture/read-control/{key}/{action}')
async def control(key: str, action: str, user=Depends(get_current_user)):
    selected = controls.get(key)
    if selected is None:
        return {'ready': False}
    if action in {'cancel', 'release'}:
        selected[action].set()
    return {'ready': selected['ready'].is_set(), 'blocks': selected['blocks']}
'''


def filesystem():
    evidence = Path(tempfile.mkdtemp(prefix='agent-read-boundaries-', dir=Path(__file__).resolve().parents[2] / 'tmp'))
    with tempfile.TemporaryDirectory(prefix='files-', dir=evidence) as directory:
        os.environ['VIBEFLOW_STORAGE_BACKEND'] = 'fs'
        os.environ['VIBEFLOW_STORAGE_FS_ROOT'] = directory
        from app.config import get_settings
        get_settings.cache_clear()
        from app.services.tools import ToolExecutor
        root = Path(directory) / 'case'
        root.mkdir()
        executor = ToolExecutor('case')
        checks = []

        def put(name, content):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf8')

        crossing = '英🙂\r\nABC'
        body = b'x' * (64 * 1024 - 2) + crossing.encode('utf8') + b'\n' + b'x' * (4 * 64 * 1024)
        (root / 'crossing.txt').write_bytes(body)
        args = {'path':'crossing.txt', 'old_text':crossing, 'new_text':'$& $1', 'reason':'literal replacement'}
        prepared, error = executor.prepare_file_proposal('propose_text_replacement', args)
        assert error is None and prepared['baseline_sha256'] == hashlib.sha256(body).hexdigest()
        assert prepared['proposal_snapshot']['kind'] == 'limit'
        checks.append('replacement_matches_split_utf8_and_crlf_above_preview_limit')
        put('repeated.txt', 'aaaaa')
        args.update(path='repeated.txt', old_text='aa', new_text='')
        _, error = executor.prepare_file_proposal('propose_text_replacement', args)
        assert error[0] == 'replacement_text_ambiguous' and '2' in error[1]
        _, error = executor.prepare_file_proposal('propose_text_replacement', dict(args, replace_all=True))
        assert error is None
        checks.append('replacement_non_overlapping_count_and_deletion')

        put('empty.txt', '')
        assert executor._tool_read_text_file('empty.txt') == ''
        checks.append('empty_file')
        put('unicode.txt', '汉字🙂' * 20_000)
        output = executor._tool_read_text_file('unicode.txt')
        assert output == ('汉字🙂' * 20_000)[:50_000] + '\n...[截断，共 200000 字节]'
        checks.append('unicode_character_preview_limit')
        put('large.txt', 'A' * 300_000)
        assert executor._tool_read_text_file('large.txt') == 'A' * 50_000 + '\n...[截断，共 300000 字节]'
        checks.append('large_file_range_preview')
        missing = executor.execute('read_text_file', {'path': 'missing.txt'})
        assert not missing['ok'] and '文件不存在: missing.txt' in missing['result']
        checks.append('missing_file')
        for value in ('../escape.txt', '/absolute.txt', 'C:/escape.txt', 'Ref/../../escape.txt'):
            for name, args in (('read_text_file', {'path': value}), ('list_files', {'subdir': value}),
                               ('search_text', {'query': 'x', 'subdir': value})):
                assert executor.execute(name, args)['ok'] is False, (name, value)
        checks.append('three_tools_reject_four_invalid_paths')
        put('Ref/visible.txt', 'first\nneedle visible\n')
        put('Ref/build/hidden.txt', 'needle excluded')
        put('.vibeflow/visible.txt', 'needle internal')
        put('.vibeflow/build/hidden.txt', 'needle nested build')
        listing = executor._tool_list_files('Ref')
        assert 'Ref/visible.txt' in listing and 'hidden' not in listing
        assert executor._tool_search_text('NEEDLE', 'Ref') == 'Ref/visible.txt:2: needle visible'
        checks.append('relative_listing_case_insensitive_search_and_build_exclusion')
        internal = executor._tool_list_files('.', True)
        assert '.vibeflow/visible.txt' in internal and '.vibeflow/build/hidden.txt' not in internal
        assert '.vibeflow' not in executor._tool_list_files()
        checks.append('explicit_internal_listing_still_excludes_build')
        put('Ref/oversized.txt', 'needle' + 'x' * 5_000_000)
        assert executor._tool_search_text('needle', 'Ref') == 'Ref/visible.txt:2: needle visible'
        checks.append('search_skips_files_over_five_mb')
        for index in range(501):
            put(f'Many/{index:04}.txt', 'x')
        listing = executor._tool_list_files('Many')
        assert len(listing.splitlines()) == 501 and '截断' in listing.splitlines()[-1]
        assert 'Many/0500.txt' not in listing
        checks.append('listing_output_cap')
        assert executor._tool_list_files('absent') == '(空目录)'
        assert executor._tool_search_text('x', 'absent') == '(无匹配)'
        checks.append('missing_directory_retains_empty_results')
        scan = os.scandir

        def check_pruned(path):
            assert Path(path).name not in {'build', 'node_modules', 'cmake', '.vibeflow'}
            return scan(path)

        put('Ref/node_modules/deep/hidden.txt', 'needle excluded')
        put('Ref/cmake/deep/hidden.txt', 'needle excluded')
        with patch('app.services.agent_file_reads.os.scandir', side_effect=check_pruned):
            assert 'hidden' not in executor._tool_list_files()
            assert 'hidden' not in executor._tool_search_text('needle')
        checks.append('heavy_directories_pruned_before_traversal')
        put('Hits/rows.txt', 'match\u2028' * 50)
        assert len(executor._tool_search_text('match', 'Hits').splitlines()) == 50
        put('Hits/rows.txt', 'match\u2028' * 51)
        hits = executor._tool_search_text('match', 'Hits').splitlines()
        assert len(hits) == 51 and hits[-2].startswith('Hits/rows.txt:50:') and '截断' in hits[-1]
        checks.append('search_output_cap_and_unicode_line_breaks')
        for index in range(7):
            put(f'Budget/{index}.txt', 'x' * 4_900_000)
        put('Budget/Target/result.txt', 'after-budget')
        assert '32 MiB' in executor._tool_search_text('after-budget', 'Budget')
        assert executor._tool_search_text('after-budget', 'Budget/Target') == 'Budget/Target/result.txt:1: after-budget'
        checks.append('aggregate_scan_budget_and_narrow_retry')
    (evidence / 'result.json').write_text(json.dumps({'passed': True, 'llm_calls': 0,
        'kind': 'actual_filesystem_tool_boundary_checks', 'checks': checks}, ensure_ascii=False, indent=2), encoding='utf8')
    print('AGENT_READ_BOUNDARIES_OK ' + str(evidence / 'result.json'))


def scope(store, database_url):
    api = IsolatedServer('agent-read-scope', app='read_fixture:app', database_url=database_url)
    bootstrap = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n' if store else ''
    (api.root / 'read_fixture.py').write_text(bootstrap + FIXTURE, encoding='utf8')
    report = dict(passed=False, llm_calls=0, storage='s3' if store else 'fs',
                  database='postgresql' if database_url else 'sqlite', checks={})
    print('AGENT_READ_SCOPE_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api, ThreadPoolExecutor(max_workers=1) as tasks:
            case = api.create_case('Actual file read scope')
            payload = 'durable-original\n' + 'x' * 180_000
            api.put('Read/large.txt', payload)
            api.put('Read/empty.txt', '')
            api.put('Read/build/excluded.txt', 'durable-excluded')

            def run(key, tool, args, *, expect=200, **options):
                return api.call('POST', f'/fixture/read/{case["id"]}/{key}', dict(tool=tool, args=args, **options),
                                api_prefix=False, expect=expect)

            def control(key, action):
                return api.call('POST', f'/fixture/read-control/{key}/{action}', api_prefix=False)

            def ready(key):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if control(key, 'status')['ready']:
                        return
                    time.sleep(.03)
                raise TimeoutError('Actual file body was not reached')

            args = {'path': 'Read/large.txt'}
            pending = tasks.submit(run, 'race', 'read_text_file', args, hold=True)
            try:
                ready('race')
                if store:
                    updated = payload.replace('original', 'replacement')
                    api.put('Read/large.txt', updated)
                else:
                    api.put('Read/sibling.txt', 'real same-case concurrent save')
                    updated = payload
                assert not pending.done()
            finally:
                control('race', 'release')
            response = pending.result(timeout=12)
            assert response['result']['ok'] and response['result']['result'].startswith(updated[:25]), response['result']
            assert response['blocks'] == 6, response['blocks']
            report['checks']['same_case_write_and_read_retry'] = {'body_blocks': 6, 'updated_content': bool(store)}

            pending = tasks.submit(run, 'stop', 'read_text_file', args, hold=True)
            try:
                ready('stop')
                assert control('stop', 'cancel') == {'ready': True, 'blocks': 1}
                assert not pending.done()
            finally:
                control('stop', 'release')
            cancelled = pending.result(timeout=12)
            assert cancelled['result']['status'] == 'cancelled' and cancelled['blocks'] == 1, cancelled
            assert run('after-stop', 'read_text_file', args)['result']['ok']
            assert run('empty', 'read_text_file', {'path':'Read/empty.txt'})['result'] == {'ok': True, 'result': ''}
            report['checks']['cancel_drain_and_retry'] = {'blocks_on_cancel': 1, 'empty_file_passed': True}

            from file_tree_lock_acceptance import set_user_status
            user_id = api.call('GET', '/auth/me')['id']
            pending = tasks.submit(run, 'revoked', 'read_text_file', args, hold=True, expect=403)
            try:
                ready('revoked')
                set_user_status(api, user_id, 'disabled')
                # Release this fixture's body from the host, independent of API authorization.
                (api.root / 'release-revoked').touch()
                denial = pending.result(timeout=12)
                assert '用户已不可用' in denial['detail'] and 'durable-' not in json.dumps(denial, ensure_ascii=False)
            finally:
                set_user_status(api, user_id, 'active')
                control('revoked', 'release')
            assert run('after-revoke', 'read_text_file', args)['result']['ok']
            report['checks']['revoked_during_read'] = {'http_status': 403, 'body_released_to_user': False}

            if store:
                result = run('catalog', 'search_text', {'subdir': 'Read', 'query': 'durable-'})
                assert result['result'] == {'ok': True, 'result': 'Read/large.txt:1: durable-replacement'}, result
                requests = result['requests']
                assert len(requests) == 2 and all(row[0] == 'GetObject' for row in requests), requests
                assert requests[0][1].endswith('/.vibeflow/live-store/head.json'), requests
                report['checks']['single_catalog_and_selected_body'] = {'catalog_gets': 1, 'file_gets': 1}

                prefix = 'workspaces/1000000/cases/1000000'
                with ThreadPoolExecutor(max_workers=5) as seeding:
                    paths = [f'Ref/{index:04}.txt' for index in range(130)] + ['Ref/build/deep/excluded.txt']
                    for _ in seeding.map(lambda name: store.raw_client.put_object(Bucket=store.bucket, Key=prefix+'/'+name, Body=b'actual'), paths):
                        pass
                response = run('legacy', 'list_files', {'subdir':'Ref'}, legacy=True)
                assert response['result']['ok'] and len(response['result']['result'].splitlines()) == 130, response
                assert response['result']['result'].endswith('Ref/0129.txt') and 'excluded' not in response['result']['result']
                pages = [row for row in response['requests'] if row[0] == 'ListObjectsV2']
                assert len(pages) >= 2 and all(row[1] == prefix+'/Ref/' and row[2] == 128 for row in pages), pages
                report['checks']['legacy_delimited_paging_and_pruning'] = {'files': 130, 'list_calls': len(pages)}
    finally:
        report['passed'] = sys.exc_info()[0] is None
        (api.root / 'read-scope-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')


async def infrastructure(storage, postgres):
    from live_llm_terminal_agent_acceptance import disposable_database

    store = nullcontext(None)
    if storage == 's3':
        from s3testsupport import ObjectStore
        store = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed', authenticated=True)
    async with disposable_database(postgres) as url:
        with store as active:
            await asyncio.to_thread(scope, active, url)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('fs', 's3'))
    parser.add_argument('--postgres', action='store_true')
    options = parser.parse_args()
    if options.scope:
        asyncio.run(infrastructure(options.scope, options.postgres))
    else:
        filesystem()
