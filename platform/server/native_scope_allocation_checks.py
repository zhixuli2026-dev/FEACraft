"""Actual creator handshake, API death and empty allocation recovery; no LLM."""
import asyncio
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from app.services.native_scope_control import ScopeControl, ScopeUnavailable, validate_prepared, validate_receipt
from app.services import native_scope_controller as protocol, owned_process, process_identity_native as identity
from app.services.windows_process_scope import WindowsScope, ProcessHandle
from app.services.windows_process_create import create


def persist(path, value):
    with path.open('x', encoding='utf8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def abandoned(preparation):
    validate_prepared(preparation)
    return {'allocation_request': preparation['request'], 'preparation': preparation['native']}


async def crash_owner(root):
    persist(root / 'crashed-owner-birth.json', identity.observe(os.getpid()))
    async def prepared(value):
        persist(root / 'crashed-owner-preparation.json', value)
    controller = ScopeControl('vibeflow-linux', 'allocate', {'seed': uuid.uuid4().hex * 2}, on_prepared=prepared)
    await controller.run()
    # Real allocation succeeded. Its descriptor is intentionally never persisted.
    # Abrupt owner exit tests recovery from only the previously durable creator.
    os._exit(81)


async def verify(root):
    report = {'passed': False, 'real_llm_calls': 0, 'mock_llm_calls': 0, 'checks': [],
              'receipts': [], 'started_at': datetime.now(timezone.utc).isoformat()}
    entries = ['native_scope_allocation_checks.py'] + ['app/services/' + name + '.py' for name in (
        'native_scope_controller', 'native_scope_control', 'process_cgroup_native', 'control_identity_reader',
        'control_identity', 'terminal_identity_probe', 'process_identity_native', 'owned_process',
        'windows_process_create', 'windows_process_scope', 'windows_process_lifetime', 'process_gate',
        'process_preparation', 'process_record', 'process_stop', 'async_ops')]
    server = Path(__file__).resolve().parent
    report['source_sha256'] = {name: hashlib.sha256((server / name).read_bytes()).hexdigest() for name in entries}
    preparations = []
    descriptors = []
    target = job = host = native = crash = None
    def record(name, kind='actual_infrastructure'):
        report['checks'].append({'name': name, 'kind': kind})
        print('ALLOCATION_PASS ' + name, flush=True)
    async def call(operation, arguments, *, on_prepared=None, failure=False):
        controller = ScopeControl('vibeflow-linux', operation, arguments, on_prepared=on_prepared)
        try:
            receipt = await controller.run()
            assert not failure, 'Expected operation to refuse'
            return receipt['result']
        except ScopeUnavailable:
            assert failure
        finally:
            if controller.receipt is not None:
                assert controller.receipt['controller_exit']['state'] == 'exited'
                assert controller.receipt['host_stop']['verified']
                assert identity.probe(controller.receipt['host']['host'])['state'] == 'exited'
                report['receipts'].append(controller.receipt)
    async def save(value):
        validate_prepared(value)
        persist(root / ('preparation-' + str(len(preparations)) + '.json'), value)
        preparations.append(value)
    try:
        seed = uuid.uuid4().hex * 2
        async def refuse(value):
            await save(value)
            await call('abandon', abandoned(value), failure=True)
            record('actual live creator prevents abandonment before permit')
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        cancelled = ScopeControl('vibeflow-linux', 'allocate', {'seed': seed}, on_prepared=refuse)
        try:
            await cancelled.run()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Prepared allocation did not cancel')
        assert cancelled.preparation is not None and cancelled.owned.closed
        assert identity.probe(cancelled.preparation['host']['host'])['state'] == 'exited'
        assert (await owned_process.probe_native('vibeflow-linux', cancelled.preparation['native']['controller']))['state'] == 'exited'
        absent = await call('abandon', abandoned(cancelled.preparation))
        assert absent['resolved_scope'] is None and absent['removed_this_call'] is False
        record('actual callback cancellation exits both controllers without creating allocation')

        descriptor = await call('allocate', {'seed': seed}, on_prepared=save)
        descriptors.append(descriptor)
        first = preparations[-1]
        second = await call('allocate', {'seed': seed}, on_prepared=save)
        descriptors.append(second)
        assert len({p['native']['reservation']['nonce'] for p in preparations}) == 3
        assert (await call('observe', {'scope': descriptor}))['state'] == 'empty'
        assert (await call('observe', {'scope': second}))['state'] == 'empty'
        record('same seed with distinct actual creators allocates distinct physical names')

        altered = copy.deepcopy(first)
        altered['native']['controller']['start_ticks'] = str(int(altered['native']['controller']['start_ticks']) + 1)
        try:
            validate_prepared(altered)
        except ValueError:
            pass
        else:
            raise AssertionError('Changed creator was accepted')
        altered = copy.deepcopy(next(r for r in report['receipts'] if r['request']['operation'] == 'allocate'))
        altered['result']['schema'] = 'vibeflow.native-cgroup.v1'
        try:
            validate_receipt(altered)
        except ValueError:
            pass
        else:
            raise AssertionError('Legacy physical name was accepted as an allocation')
        record('changed creator and allocation-to-legacy schema tampering refuse', 'protocol_fault')

        job = WindowsScope(uuid.uuid4().hex * 2)
        option = base64.b64encode(json.dumps({'scope': descriptor,
            'helper_sha256': hashlib.sha256(owned_process.GATE.with_name('process_cgroup_native.py').read_bytes()).hexdigest()}).encode()).decode()
        wsl = str(Path(os.environ['SystemRoot']) / 'System32/wsl.exe')
        target = await create(job, wsl, '-d', 'vibeflow-linux', '--exec', '/usr/bin/python3', '-I', '-S', '-u',
            owned_process.wsl_path(owned_process.GATE), '--nonce', descriptor['nonce'], '--role', 'wsl',
            '--native-scope', option, '--', '/usr/bin/python3', '-I', '-S', '-c', "print('UNRELEASED')")
        handle = ProcessHandle(int(target._transport.get_extra_info('subprocess')._handle))
        try:
            host = handle.identity()
            assert job.contains(handle)
        finally:
            handle.close()
        ready = await asyncio.wait_for(target.stdout.readline(), 15)
        assert ready.startswith(owned_process.PREFIX.encode())
        native = json.loads(ready.split(b' ', 1)[1])['identity']
        await call('abandon', abandoned(first), failure=True)
        assert (await owned_process.probe_native('vibeflow-linux', native))['state'] == 'match'
        assert (await call('observe', {'scope': descriptor}))['state'] == 'populated'
        record('empty-only abandonment refuses active allocation and preserves actual gate')
        assert (await call('retire', {'scope': descriptor}))['state'] == 'absent'
        await asyncio.wait_for(target.wait(), 15)
        assert await target.stdout.read() == b''
        assert (await owned_process.probe_native('vibeflow-linux', native))['state'] == 'exited'
        record('bound retirement revokes allocated gate without releasing its payload')

        crash = await asyncio.create_subprocess_exec(sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
            '--crash-owner', str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        code = await asyncio.wait_for(crash.wait(), 70)
        assert code == 81, 'Actual crash owner did not reach the requested boundary'
        crash_birth = json.loads((root / 'crashed-owner-birth.json').read_text(encoding='utf8'))
        report['crash_owner_birth'] = crash_birth
        report['crash_owner_exit'] = identity.probe(crash_birth)
        assert report['crash_owner_exit']['state'] == 'exited'
        recovered = json.loads((root / 'crashed-owner-preparation.json').read_text(encoding='utf8'))
        preparations.append(recovered)
        assert (await owned_process.probe_native('vibeflow-linux', recovered['native']['controller']))['state'] == 'exited'
        removed = await call('abandon', abandoned(recovered))
        assert removed['removed_this_call'] is True and removed['resolved_scope'] is not None
        assert (await call('observe', {'scope': removed['resolved_scope']}))['state'] == 'absent'
        again = await call('abandon', abandoned(recovered))
        assert again['removed_this_call'] is False and again['resolved_scope'] is None
        report['crash_owner_exit_code'] = code
        report['recovered_without_persisted_descriptor'] = removed
        record('actual owner exit before descriptor persistence recovers only empty allocation from durable creator')
        record('repeat abandonment reports absence without inventing an inode')
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'detail': str(error)[:1000]}
    finally:
        report['cleanup_errors'] = []
        if crash is not None and crash.returncode is None:
            crash.kill()
            await crash.wait()
        for descriptor in descriptors:
            try:
                await call('retire', {'scope': descriptor})
            except BaseException as error:
                report['cleanup_errors'].append(type(error).__name__)
        # Include a crash preparation even when its caller failed before reading it.
        crash_path = root / 'crashed-owner-preparation.json'
        if crash_path.exists():
            recovered = json.loads(crash_path.read_text(encoding='utf8'))
            if recovered not in preparations:
                preparations.append(recovered)
        for preparation in preparations:
            try:
                await call('abandon', abandoned(preparation))
            except BaseException as error:
                report['cleanup_errors'].append(type(error).__name__)
        if job is not None:
            job.terminate()
            await asyncio.to_thread(job.wait_empty)
            job.close()
        if target is not None:
            if target.stdin:
                target.stdin.close()
            await asyncio.wait_for(target.wait(), 10)
        if host is not None:
            report['target_host_exit'] = identity.probe(host)
        if native is not None:
            report['target_native_exit'] = await owned_process.probe_native('vibeflow-linux', native)
        report['passed'] = report['passed'] and not report['cleanup_errors']
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (root / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'evidence': str(root / 'result.json')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    if len(sys.argv) == 3 and sys.argv[1] == '--crash-owner':
        asyncio.run(crash_owner(Path(sys.argv[2])))
    else:
        root = Path(__file__).resolve().parents[2] / 'tmp' / ('native-scope-allocation-' + uuid.uuid4().hex)
        root.mkdir(exist_ok=False)
        raise SystemExit(asyncio.run(verify(root)))
