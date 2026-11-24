"""Actual WSL kernel checks for native scope admission and revocation; no LLM."""
from __future__ import annotations

import ctypes
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid


def module(name):
    path = Path(__file__).parent / 'app' / 'services' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def emit(value):
    print(json.dumps(value), flush=True)


def process(mode, descriptor=None):
    return subprocess.Popen([sys.executable, '-I', '-S', '-u', str(Path(__file__).resolve()), mode,
                             json.dumps(descriptor)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)


def line(child):
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        if not selector.select(10):
            raise TimeoutError('Actual native fixture did not become ready')
    raw = child.stdout.readline(16385)
    if not raw or len(raw) > 16384:
        raise ValueError('Invalid native fixture output')
    return json.loads(raw)


def child(mode):
    scope = module('process_cgroup_native')
    identity = module('process_identity_native')
    descriptor = json.loads(sys.argv[2])
    if mode == '--stale-fd':
        fd = os.open(scope.ROOT + '/' + scope.PARENT + '/' + descriptor['nonce'] + '/cgroup.procs', os.O_WRONLY)
        before = Path('/proc/self/cgroup').read_text()
        emit({'ready': True})
        sys.stdin.readline()
        try:
            os.write(fd, b'0')
            emit({'rejected': False})
        except OSError as error:
            emit({'rejected': True, 'errno': error.errno,
                  'membership_unchanged': Path('/proc/self/cgroup').read_text() == before})
        finally:
            os.close(fd)
        return
    if mode == '--late-join':
        try:
            scope.join(descriptor)
        except (OSError, ValueError) as error:
            emit({'rejected': True, 'error': type(error).__name__, 'payload_executed': False})
            return
        emit({'rejected': False, 'payload_executed': True})
        return
    if mode == '--member':
        scope.join(descriptor)
        self_retire_rejected = rejected(lambda: scope.retire(descriptor))
        emit({'identity': identity.observe(os.getpid()), 'self_retire_rejected': self_retire_rejected})
        # A different session/process group still inherits the cgroup.
        subprocess.Popen([sys.executable, '-I', '-S', '-u', str(Path(__file__).resolve()),
                          '--descendant', json.dumps(descriptor)], stdin=subprocess.DEVNULL)
    else:
        if mode == '--descendant':
            os.setsid()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.setgroups([]); os.setgid(65534); os.setuid(65534)
            refused = []
            for target in (scope.ROOT + '/cgroup.procs', scope.ROOT + '/' + scope.PARENT + '/cgroup.procs'):
                try:
                    fd = os.open(target, os.O_WRONLY)
                    try:
                        os.write(fd, b'0')
                    finally:
                        os.close(fd)
                    refused.append(False)
                except PermissionError:
                    refused.append(True)
            emit({'identity': identity.observe(os.getpid()), 'escape_refused': refused})
        else:
            emit({'identity': identity.observe(os.getpid())})
    time.sleep(90)  # Parent kills only its actual owned scope/creation handles.


def rejected(operation):
    try:
        operation()
    except (ValueError, OSError):
        return True
    raise AssertionError('Invalid scope was accepted')


def gate(descriptor, *, helper_hash=None, nonce=None):
    services = Path(__file__).parent / 'app' / 'services'
    encoded = base64.b64encode(json.dumps({'scope': descriptor, 'helper_sha256': helper_hash or
        hashlib.sha256((services / 'process_cgroup_native.py').read_bytes()).hexdigest()}).encode()).decode()
    payload = "import pathlib,json;print(json.dumps({'payload_executed':True,'cgroup':pathlib.Path('/proc/self/cgroup').read_text()}),flush=True)"
    return subprocess.Popen([sys.executable, '-I', '-S', '-u', str(services / 'process_gate.py'),
        '--role', 'wsl', '--nonce', nonce or descriptor['nonce'], '--native-scope', encoded,
        '--', sys.executable, '-I', '-S', '-u', '-c', payload],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def linux():
    scope = module('process_cgroup_native')
    identity = module('process_identity_native')
    library = ctypes.CDLL(None, use_errno=True)
    assert library.prctl(36, 1, 0, 0, 0) == 0  # Own/reap orphaned test descendants.
    result = {'passed': False, 'real_llm_calls': 0, 'mock_llm_calls': 0,
              'kernel': os.uname().release, 'started_at': datetime.now(timezone.utc).isoformat(),
              'checks': [], 'scope_receipts': []}
    server = Path(__file__).resolve().parent
    names = ['native_cgroup_checks.py', 'app/services/process_cgroup_native.py',
             'app/services/process_gate.py', 'app/services/process_identity_native.py']
    result['source_sha256'] = {'server/' + name: hashlib.sha256((server / name).read_bytes()).hexdigest()
                               for name in names}
    created = []
    children = []
    births = []
    try:
        first = scope.create(uuid.uuid4().hex + uuid.uuid4().hex)
        created.append(first)
        assert scope.observe(first)['state'] == 'empty'
        rejected(lambda: scope.create(first['nonce']))
        result['checks'].append('exclusive actual cgroup v2 creation')

        guard = process('--outside'); children.append(guard)
        guard_birth = line(guard)['identity']; births.append(guard_birth)
        member = process('--member', first); children.append(member)
        member_ready = line(member); member_birth = member_ready['identity']; births.append(member_birth)
        assert member_ready['self_retire_rejected']
        descendant_ready = line(member); descendant_birth = descendant_ready['identity']; births.append(descendant_birth)
        assert member_birth['session'] != descendant_birth['session']
        assert descendant_birth['uid'] == 65534 and descendant_ready['escape_refused'] == [True, True]
        result['checks'].append('self-retirement refused and actual nobody descendant cannot escape to ancestors')
        assert scope.observe(first)['state'] == 'populated'
        for key, value in (('inode', first['inode'] + 1), ('parent_inode', first['parent_inode'] + 1),
                           ('boot_id', str(uuid.uuid4())), ('nonce', '../escape')):
            rejected(lambda key=key, value=value: scope.retire({**first, key: value}))
        assert identity.probe(member_birth)['state'] == 'match'
        assert identity.probe(guard_birth)['state'] == 'match'
        result['checks'].append('mismatched boot/inodes and path traversal refused without killing')

        receipt = scope.retire(first); result['scope_receipts'].append(receipt)
        assert receipt['removed_this_call'] and scope.observe(first)['state'] == 'absent'
        member.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and identity.probe(descendant_birth)['state'] != 'exited':
            time.sleep(.01)
        assert identity.probe(member_birth)['state'] == 'exited'
        assert identity.probe(descendant_birth)['state'] == 'exited'
        assert identity.probe(guard_birth)['state'] == 'match'
        assert scope.retire(first)['removed_this_call'] is False
        result['checks'].append('actual kill of member and detached descendant; outside process survives')

        late = process('--late-join', first); children.append(late)
        result['late_join'] = line(late); assert result['late_join']['rejected']
        assert result['late_join']['payload_executed'] is False
        assert late.wait(timeout=10) == 0
        result['checks'].append('late gate rejected before payload after actual revocation')

        second = scope.create(uuid.uuid4().hex + uuid.uuid4().hex); created.append(second)
        stale = process('--stale-fd', second); children.append(stale)
        assert line(stale)['ready']
        result['scope_receipts'].append(scope.retire(second))
        stale.stdin.write(b'continue\n'); stale.stdin.flush()
        result['stale_fd'] = line(stale)
        assert result['stale_fd']['rejected'] and result['stale_fd']['membership_unchanged']
        assert stale.wait(timeout=10) == 0
        result['checks'].append('kernel rejects admission through a preopened fd after rmdir')

        replacement = scope.create(second['nonce']); created.append(replacement)
        assert replacement['inode'] != second['inode']
        rejected(lambda: scope.retire(second))
        rejected(lambda: scope.observe(second))
        late_replacement = process('--late-join', second); children.append(late_replacement)
        assert line(late_replacement)['rejected']
        assert late_replacement.wait(timeout=10) == 0
        assert scope.observe(replacement)['state'] == 'empty'
        result['scope_receipts'].append(scope.retire(replacement))
        result['checks'].append('recreated name cannot admit an old descriptor or be killed through it')

        for label, options in [('revoked', {}), ('helper_changed', {'helper_hash': '0' * 64}),
                               ('other_launch', {'nonce': '1' * 64})]:
            invalid = gate(first, **options); children.append(invalid)
            output, error = invalid.communicate(timeout=10)
            assert invalid.returncode == 125 and not output
            assert b'Process launch gate failed: ValueError' in error
        result['checks'].append('actual process gate rejects revoked, changed-helper and foreign-launch scopes before HELLO')

        waiting_scope = scope.create(uuid.uuid4().hex + uuid.uuid4().hex); created.append(waiting_scope)
        waiting = gate(waiting_scope); children.append(waiting)
        ready = waiting.stdout.readline(16385)
        assert ready.startswith(b'VIBEFLOW_PROCESS_GATE_V1 ')
        waiting_birth = json.loads(ready.split(b' ', 1)[1])['identity']; births.append(waiting_birth)
        assert scope.observe(waiting_scope)['state'] == 'populated'
        result['scope_receipts'].append(scope.retire(waiting_scope))
        output, _ = waiting.communicate(timeout=10)
        assert waiting.returncode < 0 and not output
        result['checks'].append('actual gate joined before HELLO and scope kill prevents unreleased payload')

        released_scope = scope.create(uuid.uuid4().hex + uuid.uuid4().hex); created.append(released_scope)
        released = gate(released_scope); children.append(released)
        ready = released.stdout.readline(16385)
        assert ready.startswith(b'VIBEFLOW_PROCESS_GATE_V1 ')
        births.append(json.loads(ready.split(b' ', 1)[1])['identity'])
        # Keep control open while the payload finishes; EOF is a real stop signal
        # in this protocol, so communicate(input) is intentionally not used here.
        released.stdin.write((json.dumps({'action': 'release', 'nonce': released_scope['nonce']}) + '\n').encode())
        released.stdin.flush()
        payload = line(released)
        released.wait(timeout=15)
        output, error = released.communicate(timeout=5)
        assert released.returncode == 0 and not output, error.decode()
        assert payload['payload_executed'] and '/' + scope.PARENT + '/' + released_scope['nonce'] in payload['cgroup']
        assert scope.observe(released_scope)['state'] == 'empty'
        result['scope_receipts'].append(scope.retire(released_scope))
        result['checks'].append('released payload in actual PID namespace inherits the admitted scope')
        result['passed'] = True
    except BaseException as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        result['cleanup_errors'] = []
        for descriptor in created:
            try:
                scope.retire(descriptor)
            except BaseException as error:
                result['cleanup_errors'].append(type(error).__name__ + ': ' + str(error))
        for handle in children:
            try:
                if handle.poll() is None:
                    handle.kill()
                handle.wait(timeout=10)
            except BaseException as error:
                result['cleanup_errors'].append(type(error).__name__ + ': ' + str(error))
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        result['process_observations'] = [{'identity': birth, 'observation': identity.probe(birth)} for birth in births]
        result['all_recorded_processes_exited'] = all(item['observation']['state'] == 'exited'
                                                      for item in result['process_observations'])
        result['passed'] = result['passed'] and not result['cleanup_errors'] and result['all_recorded_processes_exited']
        result['scope_observations'] = []
        for descriptor in created:
            try:
                result['scope_observations'].append(scope.observe(descriptor))
            except (OSError, ValueError) as error:
                result['scope_observations'].append({'state': 'unverified', 'error': type(error).__name__})
        result['passed'] = result['passed'] and all(item['state'] == 'absent' for item in result['scope_observations'])
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
    emit(result)
    return 0 if result['passed'] else 1


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == '--linux':
            return linux()
        child(sys.argv[1]); return 0
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    root = Path(__file__).resolve().parents[2] / 'tmp' / ('native-cgroup-' + uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    path = Path(__file__).resolve().as_posix()
    native = '/mnt/' + path[0].lower() + path[2:]
    completed = subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '--exec', 'python3', '-I', '-S', '-u',
                                native, '--linux'], capture_output=True, text=True, encoding='utf8')
    (root / 'result.json').write_text(completed.stdout, encoding='utf8')
    (root / 'stderr.txt').write_text(completed.stderr, encoding='utf8')
    emit({'exit_code': completed.returncode, 'evidence': str(root / 'result.json')})
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
