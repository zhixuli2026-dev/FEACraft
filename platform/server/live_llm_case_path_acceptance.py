"""Check real filesystem namespace boundaries and DeepSeek file tools in an isolated case."""
import argparse
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from urllib.parse import quote
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


@contextmanager
def alias(path, target, *, directory=False):
    path.symlink_to(target, target_is_directory=directory)
    try:
        yield
    finally:
        path.unlink()


@contextmanager
def redirected_directory(path, target):
    original = path.with_name(path.name + '-saved-for-test')
    path.rename(original)
    try:
        with alias(path, target, directory=True):
            yield
    finally:
        original.rename(path)


def verify(config):
    api = IsolatedServer('live-f32-case-path', app='case_path_fixture:app')
    (api.root / 'case_path_fixture.py').write_text(FIXTURE, encoding='utf-8')
    report = {'passed': False, 'mock_llm_calls': 0, 'provider': 'api.deepseek.com', 'model': config['model'], 'checks': []}
    print('FF_F32_EVIDENCE', api.root, flush=True)

    def read(path, *, expect=200, suffix='file', raw=False, headers=None):
        return api.call('GET', f'{api.prefix}/{suffix}?path={quote(path)}', expect=expect, raw=raw, extra_headers=headers)

    def write(path, *, expect=200):
        return api.call('PUT', api.prefix + '/file', {'path': path, 'content': 'changed own-case file'}, expect=expect)

    def frames(cid, profile, prompt, label):
        with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                          timeout=100, trust_env=False) as client:
            response = client.post(f'/conversations/{cid}/messages', json={'content': prompt,
                'provider_profile_id': profile, 'model': config['model'], 'reasoning_effort': 'off'})
            response.raise_for_status()
        assert config['key'] not in response.text
        (api.root / f'{label}-events.sse').write_text(response.text, encoding='utf-8')
        return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]

    try:
        with api:
            case = api.create_case('Current path boundary')
            other_token = api.call('POST', '/auth/register', {'email': 'neighbor-access@example.com',
                'name': 'Outside owner', 'password': 'isolated-other-123'})['token']
            foreign_headers = {'Authorization': 'Bearer ' + other_token}
            neighbor_ws = api.call('GET', '/workspaces', extra_headers=foreign_headers)[0]['id']
            neighbor = api.call('POST', f'/workspaces/{neighbor_ws}/cases', {'name': 'Neighbor path case'},
                                extra_headers=foreign_headers)
            own = 'OWN_CASE_' + uuid.uuid4().hex
            marker = 'NEIGHBOR_CASE_' + uuid.uuid4().hex
            own_file = 'Ref/data.txt'
            api.put(own_file, own)
            api.call('PUT', f"/cases/{neighbor['id']}/file", {'path': own_file, 'content': marker}, extra_headers=foreign_headers)
            actual = api.call('GET', api.prefix)
            other = api.call('GET', f"/cases/{neighbor['id']}", extra_headers=foreign_headers)
            storage_root = api.root / 'files'
            base, adjacent = [storage_root / info['storage_prefix'] for info in (actual, other)]
            assert base.is_dir() and adjacent.is_dir() and base != adjacent
            assert read(own_file)['content'] == own
            assert read(own_file, suffix='raw', expect=206, raw=True, headers={'Range': 'bytes=0-7'}) == own[:8].encode()
            report['checks'].append('normal_editor_and_partial_raw_read')

            outside = api.root / 'files-neighbor'
            outside.mkdir()
            (outside / 'data.txt').write_text('SIBLING_ROOT', encoding='utf-8')
            with alias(base / 'Ref' / 'outside-case.txt', adjacent / own_file), \
                 alias(base / 'Ref' / 'outside-root', outside, directory=True), \
                 alias(base / 'Ref' / 'missing-link.txt', adjacent / 'Ref' / 'uncreated' / 'target.txt'):
                for path in ('Ref/outside-case.txt', 'Ref/outside-root/data.txt', 'Ref/missing-link.txt'):
                    read(path, expect=422)
                    read(path, suffix='raw', expect=422, raw=True)
                    read(path, suffix='file:version', expect=422)
                write('Ref/outside-root/new/subdirectory/file.txt', expect=422)
                write('Ref/missing-link.txt', expect=422)
                assert not (outside / 'new').exists() and not (adjacent / 'Ref' / 'uncreated').exists()
            report['checks'].append('editor_range_version_and_missing_parent_remain_at_declared_boundaries')

            assert base.parents[1] != adjacent.parents[1]
            for declared, target in ((base, adjacent), (base.parents[1], adjacent.parents[1])):
                with redirected_directory(declared, target):
                    for suffix in ('file', 'raw', 'file:version'):
                        read(own_file, suffix=suffix, expect=409, raw=suffix == 'raw')
                    api.call('GET', api.prefix + '/tree', expect=409)
                    write(own_file, expect=409)
                assert read(own_file)['content'] == own
                assert api.call('GET', f"/cases/{neighbor['id']}/file?path={quote(own_file)}",
                                extra_headers=foreign_headers)['content'] == marker
            report['checks'].append('case_and_workspace_root_links_refused_and_original_roots_recover')

            read(own_file, expect=404, headers=foreign_headers)
            report['checks'].append('other_account_cannot_read_the_case')

            profile = api.call('POST', '/provider-profiles', {'name': 'Actual DeepSeek path acceptance',
                'provider': 'deepseek', 'base_url': config['base_url'], 'default_model': config['model'],
                'api_key': config['key']})['id']
            with alias(base / 'Ref' / 'inside.txt', base / own_file), \
                 alias(base / 'Ref' / 'blocked.txt', adjacent / own_file):
                assert read('Ref/inside.txt')['content'] == own
                for label, path, allowed in (('inside', 'Ref/inside.txt', True), ('blocked', 'Ref/blocked.txt', False)):
                    cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': label,
                        'provider_profile_id': profile, 'model': config['model'], 'permission_mode': 'plan'})['id']
                    events = frames(cid, profile, f'请只调用一次 read_text_file 工具读取相对路径 {path}。'
                        '成功时返回文件内容，失败时说明无法读取。收到工具结果后直接回答，别调用其他工具。', label)
                    calls = [frame for frame in events if frame['type'] == 'tool.result' and frame['tool'] == 'read_text_file']
                    assert len(calls) == 1 and calls[0]['ok'] is allowed, calls
                    assert marker not in json.dumps(events)
                    if allowed:
                        assert own in calls[0]['result']
                    else:
                        assert 'escapes storage prefix' in calls[0]['result']
                    assert any(frame['type'] == 'run.completed' for frame in events)
            report['checks'].append('actual_deepseek_uses_inside_alias_and_rejects_cross_case_alias')
        report.update(passed=True, isolated_api_exited=api.proc.poll() is not None)
    finally:
        receipt = api.root / 'upstream.jsonl'
        rows = [json.loads(line) for line in receipt.read_text(encoding='utf-8').splitlines()] if receipt.exists() else []
        report['http_200'] = sum(row['kind'] == 'http' and row['status'] == 200 for row in rows)
        destination = api.root / 'live-result.json'
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('FF_F32_RESULT', destination, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    args = parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)))
