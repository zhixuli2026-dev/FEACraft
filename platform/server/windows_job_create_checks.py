"""Actual Windows Job-at-creation, owner death and overlapped stdio; no LLM."""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services import process_identity_native as identity
from app.services import windows_process_create as creation
from app.services.windows_process_scope import WindowsScope, ProcessHandle, ExtendedLimits, _kernel, _require

BASE = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
SCRIPT = str(Path(__file__).resolve())


def child():
    kernel = _kernel()
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    job = _require(kernel.OpenJobObjectW(0x4, False, sys.argv[2]))
    try:
        member = wintypes.BOOL()
        _require(kernel.IsProcessInJob(kernel.GetCurrentProcess(), job, ctypes.byref(member)))
        assert member.value
    finally:
        _require(kernel.CloseHandle(job))  # Do not prolong Job lifetime after owner exit.
    Path(sys.argv[3]).write_text('payload actually entered', encoding='utf8')
    print(json.dumps({'birth': identity.observe(os.getpid()), 'in_job': True,
                      'environment': os.environ.get('VIBEFLOW_ATOMIC_TEST'), 'cwd': os.getcwd()}), flush=True)
    data = sys.stdin.buffer.read(131072)
    sys.stdout.buffer.write(data); sys.stdout.buffer.flush()


async def crashing_owner(root):
    scope = WindowsScope(uuid.uuid4().hex + uuid.uuid4().hex)
    original = creation._create_process
    def after_actual_creation(*args, **kwargs):
        result = original(*args, **kwargs)
        handle = ProcessHandle(result[0])
        assert scope.contains(handle)
        document = {'birth': handle.identity(), 'already_in_job': True,
                    'owner': identity.observe(os.getpid()), 'job': scope.name}
        (root / 'owner-died-after-create.json').write_text(json.dumps(document), encoding='utf8')
        # Real owner exits inside the production native create call, before
        # Popen/asyncio receives its creation handle or any registration runs.
        os._exit(81)
    creation._create_process = after_actual_creation
    await creation.create(scope, BASE, '-I', '-S', '-u', SCRIPT, '--child', scope.name,
                          str(root / 'orphan-payload.txt'), cwd=str(root))
    raise AssertionError('Actual owner death did not occur')


async def verify(root):
    result = {'passed': False, 'real_llm_calls': 0, 'mock_llm_calls': 0, 'checks': [],
              'started_at': datetime.now(timezone.utc).isoformat(), 'observations': []}
    births = []
    scopes = []
    processes = []
    outsider = None
    try:
        outsider = subprocess.Popen([BASE, '-I', '-S', '-c', 'import time;time.sleep(120)'],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        outside_birth = identity.observe(outsider.pid); births.append(outside_birth)
        scope = WindowsScope(uuid.uuid4().hex + uuid.uuid4().hex); scopes.append(scope)
        environment = dict(os.environ, VIBEFLOW_ATOMIC_TEST='中文 spaces & < > " \\')
        proc = await creation.create(scope, BASE, '-I', '-S', '-u', SCRIPT, '--child', scope.name,
                                     str(root / 'stdio payload 中文.txt'), cwd=str(root), env=environment)
        processes.append(proc)
        retained = ProcessHandle(int(proc._transport.get_extra_info('subprocess')._handle))
        try:
            assert scope.contains(retained)
            birth = retained.identity(); births.append(birth)
            hello = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
            assert hello['birth'] == birth and hello['in_job']
            assert hello['environment'] == environment['VIBEFLOW_ATOMIC_TEST']
            assert Path(hello['cwd']).resolve() == root.resolve()
            payload = bytes(range(256)) * 512
            proc.stdin.write(payload); await proc.stdin.drain()
            assert await asyncio.wait_for(proc.stdout.readexactly(len(payload)), 10) == payload
            assert await asyncio.wait_for(proc.wait(), 10) == 0
            assert await proc.stderr.read() == b''
            await asyncio.to_thread(scope.retire)
            assert scope.observer.summary()['complete']
            result['checks'].append('Job membership from first payload instruction; Unicode argv/env/cwd and 128KiB actual overlapped stdio')
        finally:
            retained.close()

        rejected_scope = WindowsScope(uuid.uuid4().hex + uuid.uuid4().hex); scopes.append(rejected_scope)
        limits = ExtendedLimits(); limits.basic.flags = 0x2000 | 0x8; limits.basic.active_process_limit = 0
        _require(rejected_scope.kernel.SetInformationJobObject(rejected_scope.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        try:
            rejected = await creation.create(rejected_scope, BASE, '-I', '-S', '-u', SCRIPT, '--child',
                rejected_scope.name, str(root / 'must-not-exist.txt'), cwd=str(root))
            processes.append(rejected)
        except OSError:
            pass
        else:
            raise AssertionError('Kernel Job admission failure was bypassed')
        assert not (root / 'must-not-exist.txt').exists()
        assert rejected_scope.active_processes() == 0
        result['checks'].append('actual kernel Job limit rejects creation without an unassigned fallback')

        owner = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', SCRIPT, '--crash-owner', str(root)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            _, stderr = await asyncio.to_thread(owner.communicate, timeout=20)
            assert owner.returncode == 81, stderr.decode('utf8', errors='replace')
        finally:
            if owner.poll() is None:
                owner.kill(); owner.wait(timeout=5)
        recorded = json.loads((root / 'owner-died-after-create.json').read_text(encoding='utf8'))
        births.extend([recorded['birth'], recorded['owner']])
        assert recorded['already_in_job']
        deadline = time.monotonic() + 10
        while identity.probe(recorded['birth'])['state'] != 'exited' and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert identity.probe(recorded['birth'])['state'] == 'exited'
        assert identity.probe(outside_birth)['state'] == 'match'
        result['checks'].append('actual owner exits after CreateProcess before Popen adoption; bound child exits and outside process survives')

        from app.services.owned_process import OwnedProcess
        cancelled = OwnedProcess()
        async def cancel_created(value):
            if value['stage'] == 'created':
                assert cancelled.scope.contains(cancelled.handle)
                births.append(value['host'])
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
        cancelled.on_preparation = cancel_created
        try:
            await cancelled.prepare([BASE, '-I', '-S', '-c',
                "from pathlib import Path;Path('unreleased.txt').write_text('unexpected')"],
                cwd=root, env=None, runtime='windows')
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Actual preparing task was not cancelled')
        assert cancelled.closed and cancelled.proc.returncode is not None
        assert not (root / 'unreleased.txt').exists()
        assert identity.probe(births[-1])['state'] == 'exited'
        result['checks'].append('OwnedProcess created callback already sees Job membership; actual cancellation stops unreleased host')

        owned = OwnedProcess()
        try:
            await owned.prepare([BASE, '-I', '-S', '-c', "print('ACTUAL_PAYLOAD_FINISHED')"],
                                cwd=root, env=None, runtime='windows')
            births.append(owned.record['host'])
            await owned.verify_registration(); await owned.release()
            assert (await asyncio.wait_for(owned.proc.stdout.read(), 10)).strip() == b'ACTUAL_PAYLOAD_FINISHED'
            assert await owned.wait_root_exit(10) == 0
            receipt = await owned.finish()
            assert receipt['verified']
            result['normal_stop'] = receipt
        finally:
            if not owned.closed:
                await owned.abort()
        result['checks'].append('actual OwnedProcess prepare/register/release/output/complete remains fully verified')
        result['passed'] = True
    except BaseException as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        result['cleanup_errors'] = []
        for scope in scopes:
            try:
                scope.terminate()
                await asyncio.to_thread(scope.wait_empty)
                scope.close()
            except BaseException as error:
                result['cleanup_errors'].append(type(error).__name__ + ': ' + str(error))
        for proc in processes:
            try:
                if proc.stdin is not None: proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 10)
            except BaseException as error:
                result['cleanup_errors'].append(type(error).__name__ + ': ' + str(error))
        if outsider is not None:
            if outsider.poll() is None: outsider.kill()
            outsider.wait(timeout=10)
        result['observations'] = [{'birth': birth, 'observation': identity.probe(birth)} for birth in births]
        result['all_recorded_processes_exited'] = all(row['observation']['state'] == 'exited' for row in result['observations'])
        result['passed'] = result['passed'] and not result['cleanup_errors'] and result['all_recorded_processes_exited']
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        server = Path(__file__).resolve().parent
        names = ['windows_job_create_checks.py', 'app/services/windows_process_create.py',
                 'app/services/windows_process_scope.py', 'app/services/windows_process_lifetime.py',
                 'app/services/process_identity_native.py', 'app/services/owned_process.py',
                 'app/services/process_gate.py', 'app/services/process_preparation.py', 'app/services/async_ops.py']
        result['source_sha256'] = {'server/' + name: hashlib.sha256((server / name).read_bytes()).hexdigest() for name in names}
        (root / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps({'passed': result['passed'], 'evidence': str(root / 'result.json')}), flush=True)
    return 0 if result['passed'] else 1


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == '--child':
            child(); return 0
        if sys.argv[1] == '--crash-owner':
            asyncio.run(crashing_owner(Path(sys.argv[2]))); return 1
        raise ValueError('Unexpected fixture argument')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    root = Path(__file__).resolve().parents[2] / 'tmp' / ('windows-job-create-' + uuid.uuid4().hex)
    root.mkdir(exist_ok=False)
    return asyncio.run(verify(root))


if __name__ == '__main__':
    raise SystemExit(main())
