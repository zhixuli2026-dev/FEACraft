"""Resource admission and shutdown using the real public runtime capability route."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import time
import uuid

import httpx

from app.services import process_identity_native as identity
from app.services.owned_process import probe_native
from testsupport import IsolatedServer


FIXTURE = r'''
import asyncio
import json
from pathlib import Path
import sys
import time

from fastapi import Depends
from sqlalchemy import select

from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import NativeRequest
from app.routers import runtime
from app.config import get_settings
from app.services import native_request_execution, native_request_records, process_identity_native
from app.services.owned_process import wsl_path

root = Path(__file__).parent
command = native_request_execution.RequestExecutor._command
complete = native_request_records.complete

def launch(self, purpose, parameters):
    argv, seconds, limit = command(self, purpose, parameters)
    if (root/'hold-payload').exists():
        source = Path(process_identity_native.__file__)
        if self.runtime == 'wsl':
            payload = ['wsl.exe', '-d', self.profile['distro'], '--user', 'nobody', '--exec',
                       'python3', '-I', '-S', wsl_path(root/'held_runtime.py')]
            paths = [wsl_path(source), wsl_path(root)]
        else:
            payload = [sys._base_executable, '-I', '-S', str(root/'held_runtime.py')]
            paths = [str(source), str(root)]
        return [*payload, *paths, self.owner.id], seconds, limit
    if self.kind == 'runtime.python_probe' and (root/'fail-python').exists():
        return [sys._base_executable, '-I', '-S', '-c', 'raise SystemExit(49)'], seconds, limit
    return argv, seconds, limit

async def ending(owner, *args, **kwargs):
    if (root/'hold-commit').exists():
        (root/('commit-'+owner.id)).touch()
        end = asyncio.get_running_loop().time() + 20
        while (root/'hold-commit').exists() and asyncio.get_running_loop().time() < end:
            await asyncio.sleep(.03)
    return await complete(owner, *args, **kwargs)

native_request_execution.RequestExecutor._command = launch
native_request_records.complete = ending

write_bytes = Path.write_bytes
def pause_probe_write(path, data):
    count = write_bytes(path, data)
    if path.parent.name == 'capabilities' and path.name.startswith('probe-') and (root/'hold-storage').exists():
        (root/('ready-'+path.stem)).write_text(str(path), encoding='utf8')
        end = time.monotonic()+25
        while (root/'hold-storage').exists() and time.monotonic()<end:
            time.sleep(.025)
    return count
Path.write_bytes = pause_probe_write

@app.post('/fixture/monitor-deadline')
async def monitor_deadline(user=Depends(get_current_user)):
    get_settings().storage_measurement_timeout_seconds = 2
    return {'seconds': 2}

@app.get('/fixture/requests')
async def requests(user=Depends(get_current_user), db=Depends(get_db)):
    rows = (await db.scalars(select(NativeRequest).order_by(NativeRequest.created_at))).all()
    return [native_request_records.public(row) for row in rows]

@app.post('/fixture/deadline')
async def deadline(user=Depends(get_current_user)):
    runtime._CAPABILITIES_SECONDS = 8
    return {'seconds': 8}

@app.post('/fixture/default-deadline')
async def default_deadline(user=Depends(get_current_user)):
    runtime._CAPABILITIES_SECONDS = 65
    return {'seconds': 65}
'''

PAYLOAD = r'''
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

source, directory, request_id = sys.argv[1:4]
if len(sys.argv) == 4:
    child = subprocess.Popen([sys.executable, '-I', '-S', __file__, *sys.argv[1:4], 'descendant'])
    child.wait(timeout=60)
else:
    spec = importlib.util.spec_from_file_location('process_identity', source)
    identity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity)
    path = Path(directory)/('child-'+request_id)
    staging = path.with_suffix('.staging')
    staging.write_text(json.dumps(identity.observe(os.getpid())), encoding='utf-8')
    staging.replace(path)
    time.sleep(60)
'''


def poll(fetch, predicate, seconds=25):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = fetch()
        if predicate(value):
            return value
        time.sleep(.05)
    raise TimeoutError(f'Waited {seconds}s for runtime capabilities: {value}')


def raw_request(api):
    stream = socket.create_connection(('127.0.0.1', api.port), timeout=5)
    payload = ('GET /api/v1/runtime/capabilities HTTP/1.1\r\nHost: 127.0.0.1\r\n'
               f'Authorization: Bearer {api.token}\r\nConnection: close\r\n\r\n')
    stream.sendall(payload.encode('ascii'))
    return stream


def birth_state(distro, birth):
    observed = identity.probe(birth) if birth['platform'] == 'windows' else asyncio.run(probe_native(distro, birth))
    return observed['state']


def receipts(api, rows, *, target):
    result = []
    for row in rows:
        child, = row['children']['children'].values()
        assert row['phase'] == target and row['cleanup_verified'] and row['ended_at'], row
        assert child['stop']['verified'], child
        birth = child['identity']
        distro = birth['native']['distribution'] if birth['native'] else None
        assert birth_state(None, birth['host']) == 'exited', child
        if distro is not None:
            assert birth_state(distro, birth['native']['process']) == 'exited', child
        descendant = json.loads((api.root/('child-'+row['id'])).read_text(encoding='utf-8'))
        assert birth_state(distro, descendant) == 'exited', descendant
        result.append({'id': row['id'], 'kind': row['kind'], 'phase': target, 'error': row['error'],
                       'stop_reason': child['stop']['reason'], 'verified': True,
                       'births': birth, 'descendant': descendant, 'observed': 'exited'})
    return result


def verify():
    harness = IsolatedServer('runtime-capabilities-budget', app='runtime_capability_fixture:app')
    root = harness.root
    (root/'runtime_capability_fixture.py').write_text(FIXTURE, encoding='utf-8')
    (root/'held_runtime.py').write_text(PAYLOAD, encoding='utf-8')
    result = {'model_calls': 0, 'mock_llm_calls': 0, 'checks': {}}
    checks = result['checks']
    owner = None

    def write_result():
        (root/'runtime-capabilities-budget.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')

    try:
        with harness as api:
            api.call('GET', '/runtime/capabilities', expect=401)
            api.token = api.call('POST', '/auth/register', {
                'email': uuid.uuid4().hex+'@example.com', 'password': 'runtime-test-123', 'name': 'Runtime budget'})['token']

            def rows():
                return api.call('GET', '/fixture/requests', api_prefix=False)

            def descendants(selected):
                return all((root/('child-'+row['id'])).exists() for row in selected)

            def request(timeout=30):
                return httpx.get(api.base+'/api/v1/runtime/capabilities',
                                 headers={'Authorization': 'Bearer '+api.token}, timeout=timeout, trust_env=False)

            assert not rows()
            checks['unauthenticated_status'] = 401
            (root/'hold-payload').touch()
            (root/'hold-commit').touch()
            with raw_request(api) as first, raw_request(api) as second:
                active = poll(rows, lambda current: len(current) == 4 and descendants(current))
                owner = active[0]['owner_identity']
                assert all(row['phase'] == 'active' and row['cleanup_verified'] is False for row in active)
                started = time.monotonic()
                try:
                    overload = request(timeout=3)
                except httpx.ReadTimeout:
                    leaked = poll(rows, lambda current: len(current) > 4)
                    checks['reproduction'] = {'third_request_waited_seconds': time.monotonic()-started,
                                              'active_owners': len(leaked), 'initial_owners': len(active)}
                    raise AssertionError('Third public probe started additional owned native work')
                assert overload.status_code == 429 and overload.json()['detail']['code'] == 'runtime_capabilities_busy', overload.text
                assert overload.headers['retry-after'] == '1' and len(rows()) == 4
                checks['overload'] = {'status': overload.status_code, 'owners': len(active),
                                      'elapsed': time.monotonic()-started, 'retry_after': overload.headers['retry-after']}
                first.shutdown(socket.SHUT_RDWR)
                second.shutdown(socket.SHUT_RDWR)
                poll(lambda: list(root.glob('commit-*')), lambda markers: len(markers) == 4)
                during = request(timeout=3)
                assert during.status_code == 429 and len(rows()) == 4
                checks['overload_during_cancel_commit'] = during.status_code
            (root/'hold-commit').unlink()
            cancelled = poll(rows, lambda current: all(row['phase'] == 'cancelled' for row in current))
            checks['disconnect'] = receipts(api, cancelled, target='cancelled')
            ids = {row['id'] for row in cancelled}

            api.call('POST', '/fixture/deadline', api_prefix=False)
            started = time.monotonic()
            expired = request(timeout=30)
            elapsed = time.monotonic()-started
            assert expired.status_code == 504 and expired.json()['detail']['code'] == 'runtime_capabilities_timeout', expired.text
            assert 7.8 <= elapsed < 28, elapsed
            timed_out = [row for row in rows() if row['id'] not in ids]
            assert len(timed_out) == 2 and all(row['error'] == 'TimeoutError' for row in timed_out), timed_out
            checks['timeout'] = {'status': expired.status_code, 'elapsed': elapsed,
                                 'receipts': receipts(api, timed_out, target='failed')}
            (root/'hold-payload').unlink()
            api.call('POST', '/fixture/default-deadline', api_prefix=False)

            probe_root = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/'.vibeflow'/'capabilities'
            probe_root.parent.mkdir(parents=True, exist_ok=True)
            if probe_root.is_dir():
                probe_root.rmdir()
            probe_root.touch()
            (root/'fail-python').touch()
            failed = request().raise_for_status().json()
            assert failed['python']['reachable'] is False and failed['python']['error'] == 'ChildExecutionError', failed
            assert failed['wsl']['reachable'] and not failed['storage']['ok'] and 'FileExistsError' in failed['storage']['error'], failed
            checks['degraded'] = {'python': failed['python']['error'], 'wsl': True,
                                  'storage': failed['storage']['error'].split(':')[0]}
            probe_root.unlink()
            (root/'fail-python').unlink()

            normal = request().raise_for_status().json()
            assert normal['python']['reachable'] and normal['python']['packages']['fastapi'], normal
            assert normal['wsl']['reachable'] and normal['wsl']['openfoam'] and normal['wsl']['wmake'], normal
            assert normal['storage']['ok'] and normal['database']['ok'] and not list(probe_root.glob('probe-*.tmp'))
            assert normal['cli_agent_details'] and normal['environment_registry'] and normal['stage_checks']
            for name, path in normal['cli_agents'].items():
                assert normal['cli_agent_details'][name]['path'] == path, normal
            finished = {row['id']: row for row in rows()}
            for kind in ('python', 'wsl'):
                row = finished[normal[kind]['request_id']]
                assert row['phase'] == 'succeeded' and row['cleanup_verified'] and row['case_id'] is None
            checks['normal'] = {'status': 200, 'python': normal['python']['version'],
                                'distro': normal['wsl']['distro'], 'openfoam': normal['wsl']['openfoam_version'],
                                'storage': normal['storage']['ok'], 'database': normal['database']['ok'],
                                'cli_paths': sorted(normal['cli_agents'])}
            assert all(row['phase'] != 'active' for row in finished.values())
            checks['terminal_owner_count'] = len(finished)
            api.call('POST', '/fixture/monitor-deadline', api_prefix=False)
            blocked = root/'hold-storage'
            blocked.touch()
            def monitor(path):
                return httpx.get(api.base+'/api/v1/runtime/'+path,
                                 headers={'Authorization':'Bearer '+api.token},timeout=12,trust_env=False)
            with ThreadPoolExecutor(max_workers=2) as pool:
                calls = [pool.submit(monitor,path) for path in ('monitor','metrics')]
                try:
                    started = time.monotonic()
                    markers = poll(lambda:list(root.glob('ready-probe-*')),lambda items:len(items)==2,seconds=8)
                    files = [Path(item.read_text(encoding='utf8')) for item in markers]
                    assert all(path.read_bytes()==b'vibeflow-storage-probe' for path in files)
                    time.sleep(max(0,2.15-(time.monotonic()-started)))
                    assert all(not call.done() for call in calls)
                    over=monitor('monitor')
                    assert over.status_code==429 and over.json()['detail']['code']=='runtime_monitor_busy'
                    assert api.call('GET','/api/health',api_prefix=False)['ok']
                    identity_check=api.call('POST','/auth/register',{
                        'email':uuid.uuid4().hex+'@example.com','password':'runtime-test-123','name':'Parallel storage check'})
                    assert identity_check['user']['id']>0 and len(list(root.glob('ready-probe-*')))==2
                finally:
                    blocked.unlink(missing_ok=True)
                expired=[call.result(timeout=10) for call in calls]
            assert all(response.status_code==504 and response.json()['detail']['code']=='runtime_monitor_timeout'
                       for response in expired)
            assert not any(path.exists() for path in files)
            recovered=monitor('monitor')
            assert recovered.status_code==200 and recovered.json()['storage']['ok']
            metrics = monitor('metrics')
            assert metrics.status_code==200 and 'vibeflow_storage_up 1' in metrics.text
            checks['monitor_storage_drain']={'real_temporary_writes':len(files),'statuses':[r.status_code for r in expired],
                'overload_during_drain':over.status_code,'unrelated_sql_write':True,'subsequent_status':recovered.status_code,
                'metrics_retried':True}
        # The Windows virtualenv launcher can exit before its API child.
        poll(lambda: identity.probe(owner), lambda observed: observed['state'] == 'exited', seconds=12)
        checks['api_owner_exit'] = True
        result['passed'] = True
    except BaseException as error:
        result.update(passed=False, error=type(error).__name__, detail=str(error))
        raise
    finally:
        (root/'hold-commit').unlink(missing_ok=True)
        write_result()
    print(f'RUNTIME_CAPABILITIES_BUDGET_OK {root}', flush=True)


if __name__ == '__main__':
    import sys
    from testsupport import SERVER
    sys.path.insert(0, str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    verify()
