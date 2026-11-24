"""Real HTTP dependency observations, private publications and read/write races."""
from concurrent.futures import ThreadPoolExecutor
import json

from default_workspace_smoke import command
from job_release_smoke import wait_file
from professional_launch_smoke import settled
from testsupport import IsolatedServer

FIXTURE = '''
import time
from pathlib import Path
from app.services import dependency_status
root=Path(__file__).parent
original=dependency_status.inspect_bundle
def inspect(*args):
 if (root/'hold').exists():
  (root/'ready').touch()
  deadline=time.monotonic()+30
  while not (root/'release').exists():
   if time.monotonic()>deadline: raise TimeoutError('dependency read fixture')
   time.sleep(.02)
 return original(*args)
dependency_status.inspect_bundle=inspect
from app.main import app
'''


def main(database_url=None):
    api = IsolatedServer('dependency-status', app='dependency_fixture:app', database_url=database_url)
    (api.root/'dependency_fixture.py').write_text(FIXTURE, encoding='utf8')
    checks = []
    with api:
        api.create_case('Dependency graph')
        endpoint = api.prefix+'/dependencies'
        initial = api.call('GET', endpoint)
        assert initial['recompute'] == ['geometry', 'mesh', 'solve', 'postprocess', 'report'], initial
        assert initial['needs_inspection'] == []
        api.call('GET', '/cases/99999999/dependencies', expect=404)
        api.call('GET', endpoint+'?run_dir=../escape', expect=400)
        checks.append('missing_core_order_owned_case_and_run_directory_validation')
        api.put('Ref/input.txt', 'input one')
        body = {'type': 'convert', 'command': command("from pathlib import Path; Path('Ref/result.txt').write_text('result')")}
        job = settled(api, api.call('POST', api.prefix+'/jobs', body)['id'])
        assert job['status'] == 'succeeded', job
        identifier = job['attempt']['outputs']['publication']['id']
        root = next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        def status():
            result = api.call('GET', endpoint)
            return result, next(n for n in result['publications'] if n['publication_id'] == identifier)
        result, node = status()
        assert node['status'] == 'fresh', result
        checks.append('real_committed_typed_publication_is_fresh')
        (api.root/'hold').touch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(api.call, 'GET', endpoint, expect=409)
            wait_file(api.root/'ready', seconds=15)
            # This must complete before release: validation holds no write lock.
            api.put('Ref/input.txt', 'input two')
            (api.root/'release').touch()
            conflict = future.result(timeout=20)
            assert conflict['detail']['code'] == 'dependency_changed', conflict
        (api.root/'hold').unlink()
        result, node = status()
        assert node['status'] == 'stale' and node['kind'] in result['recompute'], result
        assert any(r['code'] == 'source_changed' for r in node['reasons'])
        assert api.call('GET', f'/jobs/{job["id"]}')['status'] == 'succeeded'
        checks.append('concurrent_edit_is_not_blocked_and_mixed_graph_returns_conflict')
        checks.append('source_change_explains_recompute_without_rewriting_success_history')
        bundle = root/'.vibeflow/publications'/identifier
        (bundle/'files/Ref/result.txt').write_text('corrupted immutable output')
        result, node = status()
        assert node['status'] == 'unverifiable' and node['kind'] in result['needs_inspection'], result
        assert node['kind'] not in result['recompute']
        checks.append('corrupt_publication_is_unverifiable_not_fresh_or_automatic_recompute')
        api.create_case('Empty publication')
        api.call('GET', endpoint, expect=404)
        checks.append('another_user_cannot_read_case_dependency_graph')
        empty = settled(api, api.call('POST', api.prefix+'/jobs',
            {'type': 'convert', 'command': command("print('NO_OUTPUT')")})['id'])
        assert empty['status'] == 'succeeded', empty
        observed = api.call('GET', api.prefix+'/dependencies')
        assert len(observed['publications']) == 1 and observed['publications'][0]['status'] == 'fresh', observed
        checks.append('empty_typed_publication_is_valid')
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(api.root)}
    (api.root/'dependency-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print('DEPENDENCY_STATUS_OK '+json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    main()
