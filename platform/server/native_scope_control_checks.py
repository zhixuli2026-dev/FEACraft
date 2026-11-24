"""Actual owned Windows/WSL cgroup control and revocation; no model inference."""
import asyncio
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from app.services.native_scope_control import ScopeControl, ScopeUnavailable, validate_receipt
from app.services import owned_process, process_identity_native as identity
from app.services.windows_process_scope import WindowsScope, ProcessHandle
from app.services.windows_process_create import create


async def verify(root):
    report = {'passed': False, 'real_llm_calls': 0, 'mock_llm_calls': 0, 'checks': [],
              'receipts': [], 'started_at': datetime.now(timezone.utc).isoformat()}
    server = Path(__file__).resolve().parent
    names = ['native_scope_control_checks.py'] + ['app/services/' + name + '.py' for name in (
        'native_scope_controller','native_scope_control','process_cgroup_native','control_identity_reader','control_identity',
        'process_identity_native','owned_process','windows_process_create','windows_process_scope','windows_process_lifetime',
        'process_gate','process_preparation','process_record','process_stop','async_ops')]
    report['source_sha256'] = {'server/' + name: hashlib.sha256((server / name).read_bytes()).hexdigest() for name in names}
    descriptor = None
    target = None
    job = None
    host = None
    native = None
    async def call(operation, arguments, *, failure=False):
        control = ScopeControl('vibeflow-linux', operation, arguments)
        try:
            receipt = await control.run()
            assert not failure
            return receipt['result']
        except ScopeUnavailable:
            assert failure
        finally:
            if control.receipt is not None:
                assert control.receipt['controller_exit']['state'] == 'exited'
                assert control.receipt['host_stop']['verified']
                assert identity.probe(control.receipt['host']['host'])['state'] == 'exited'
                report['receipts'].append(control.receipt)
    try:
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        descriptor = await call('create', {'launch_nonce': nonce})
        assert descriptor['nonce'] == nonce
        assert (await call('observe', {'scope': descriptor}))['state'] == 'empty'
        report['checks'].append({'name': 'actual controlled creation and observation; both helper lifetimes verified', 'kind': 'actual_infrastructure'})
        for key, value in [('controller_exit', {'state': 'match', 'reason': 'birth_and_scope_match'}),
                           ('membership', '0::/vibeflow-native/' + nonce + '\n')]:
            altered = copy.deepcopy(report['receipts'][0]); altered[key] = value
            try:
                validate_receipt(altered)
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid durable controller evidence was accepted')
        altered = copy.deepcopy(report['receipts'][0]); altered['host']['launch_nonce'] = '0' * 64
        try:
            validate_receipt(altered)
        except ValueError:
            pass
        else:
            raise AssertionError('Foreign controller host was accepted')
        report['checks'].append({'name': 'durable receipt rejects live controller, target membership and foreign host binding', 'kind': 'protocol_fault'})

        job = WindowsScope(uuid.uuid4().hex + uuid.uuid4().hex)
        scope_option = base64.b64encode(json.dumps({'scope': descriptor,
            'helper_sha256': hashlib.sha256(owned_process.GATE.with_name('process_cgroup_native.py').read_bytes()).hexdigest()}).encode()).decode()
        wsl = str(Path(os.environ['SystemRoot']) / 'System32/wsl.exe')
        target = await create(job, wsl, '-d', 'vibeflow-linux', '--exec', '/usr/bin/python3', '-I', '-S', '-u',
            owned_process.wsl_path(owned_process.GATE), '--nonce', nonce, '--role', 'wsl', '--native-scope', scope_option,
            '--', '/usr/bin/python3', '-I', '-S', '-c', "print('UNRELEASED_PAYLOAD_MUST_NOT_EXECUTE')")
        handle = ProcessHandle(int(target._transport.get_extra_info('subprocess')._handle))
        try:
            host = handle.identity(); assert job.contains(handle)
        finally:
            handle.close()
        ready = await asyncio.wait_for(target.stdout.readline(), 15)
        assert ready.startswith(owned_process.PREFIX.encode())
        native = json.loads(ready.split(b' ', 1)[1])['identity']
        assert (await owned_process.probe_native('vibeflow-linux', native))['state'] == 'match'
        assert (await call('observe', {'scope': descriptor}))['state'] == 'populated'
        report['checks'].append({'name': 'actual native gate joins selected cgroup and waits unreleased', 'kind': 'actual_infrastructure'})

        await call('retire', {'scope': {**descriptor, 'inode': descriptor['inode'] + 1}}, failure=True)
        assert (await owned_process.probe_native('vibeflow-linux', native))['state'] == 'match'
        assert (await call('observe', {'scope': descriptor}))['state'] == 'populated'
        report['checks'].append({'name': 'wrong scope birth refuses mutation and preserves active target', 'kind': 'protocol_fault'})

        cancelled = ScopeControl('vibeflow-linux', 'retire', {'scope': descriptor})
        cancelled_births = []
        async def cancel_before_control_release(value):
            if value['stage'] == 'created':
                cancelled_births.append(value['host'])
                asyncio.current_task().cancel(); await asyncio.sleep(0)
        cancelled.owned.on_preparation = cancel_before_control_release
        try:
            await cancelled.run()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Actual control task did not cancel')
        assert cancelled.owned.closed and len(cancelled_births) == 1
        assert identity.probe(cancelled_births[0])['state'] == 'exited'
        report['cancelled_controller_birth'] = cancelled_births[0]
        assert (await call('observe', {'scope': descriptor}))['state'] == 'populated'
        report['checks'].append({'name': 'actual pre-release control cancellation exits its host and does not revoke target', 'kind': 'actual_infrastructure'})

        changed = ScopeControl('vibeflow-linux', 'retire', {'scope': descriptor})
        changed.request['helpers']['process_cgroup_native.py'] = '0' * 64
        try:
            await changed.run()
        except ValueError:
            pass
        else:
            raise AssertionError('Changed helper selection was accepted')
        assert changed.owned.proc is None
        assert (await owned_process.probe_native('vibeflow-linux', native))['state'] == 'match'
        report['checks'].append({'name': 'changed helper digest rejects before process creation', 'kind': 'protocol_fault'})

        result = await call('retire', {'scope': descriptor})
        assert result['state'] == 'absent' and result['removed_this_call']
        assert await asyncio.wait_for(target.wait(), 15) != 0
        assert await target.stdout.read() == b''
        assert (await owned_process.probe_native('vibeflow-linux', native))['state'] == 'exited'
        assert (await call('retire', {'scope': descriptor}))['removed_this_call'] is False
        assert (await call('observe', {'scope': descriptor}))['state'] == 'absent'
        report['checks'].append({'name': 'actual controller revokes live native gate; repeat remains absent with no payload', 'kind': 'actual_infrastructure'})
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'detail': str(error)[:1000]}
    finally:
        report['cleanup_errors'] = []
        if descriptor is not None:
            try:
                report['final_scope'] = await call('retire', {'scope': descriptor})
            except BaseException as error:
                report['cleanup_errors'].append(type(error).__name__)
        if job is not None:
            try:
                job.terminate(); await asyncio.to_thread(job.wait_empty); job.close()
            except BaseException as error:
                report['cleanup_errors'].append(type(error).__name__)
        if target is not None:
            if target.stdin: target.stdin.close()
            await asyncio.wait_for(target.wait(), 10)
        if host is not None: report['target_host_exit'] = identity.probe(host)
        if native is not None: report['target_native_exit'] = await owned_process.probe_native('vibeflow-linux', native)
        report['passed'] = report['passed'] and not report['cleanup_errors']
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (root / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'evidence': str(root / 'result.json')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    root = Path(__file__).resolve().parents[2] / 'tmp' / ('native-scope-control-' + uuid.uuid4().hex)
    root.mkdir(exist_ok=False)
    raise SystemExit(asyncio.run(verify(root)))
