"""Actual Windows Job guardian ownership, API death and full member exit; no LLM."""
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from app.services import windows_scope_guardian as protocol, process_identity_native as identity
from app.services.windows_scope_guard import Guardian
from app.services.windows_process_scope import WindowsScope, ProcessHandle
from app.services import windows_process_create as creation

ENTRY = Path(__file__).resolve()
BASE = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())


def save(path, value):
    with path.open('x', encoding='utf8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush(); os.fsync(stream.fileno())


async def await_file(path, seconds=15):
    async with asyncio.timeout(seconds):
        while not path.exists():
            await asyncio.sleep(.02)
    return json.loads(path.read_text(encoding='utf8'))


def payload(root, leaf=False):
    save(root / ('leaf.json' if leaf else 'payload.json'), identity.observe(os.getpid()))
    if not leaf:
        subprocess.Popen([BASE, '-X', 'utf8', '-B', str(ENTRY), '--leaf', str(root)],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(300)


async def owner(root, mode):
    scope = WindowsScope(uuid.uuid4().hex * 2, observe=False)
    guardian = Guardian(scope, root / 'guardian')
    guardian.start()
    save(root / 'armed.json', {'request': guardian.request, 'prepared': guardian.prepared})
    if mode == 'empty':
        os._exit(81)
    if mode == 'creation_gap':
        original = creation._create_process
        def crash_after_actual_create(*args, **kwargs):
            result = original(*args, **kwargs)
            handle = ProcessHandle(result[0])
            try:
                save(root / 'created-before-adoption.json', handle.identity(allow_exited=True))
            finally:
                handle.close()
            os._exit(81)
        creation._create_process = crash_after_actual_create
    child = await creation.create(scope, BASE, '-X', 'utf8', '-B', str(ENTRY), '--payload', str(root))
    await await_file(root / 'leaf.json')
    os._exit(81)


async def verify(root):
    report = {'passed': False, 'real_llm_calls': 0, 'mock_llm_calls': 0, 'checks': [], 'receipts': [],
              'started_at': datetime.now(timezone.utc).isoformat(), 'cleanup_errors': []}
    names = ['windows_scope_guard_checks.py'] + ['app/services/' + name + '.py' for name in (
        'windows_scope_guardian', 'windows_scope_guard', 'windows_process_scope', 'windows_process_lifetime',
        'windows_process_create', 'process_identity_native')]
    report['source_sha256'] = {name: hashlib.sha256((ENTRY.parent / name).read_bytes()).hexdigest() for name in names}
    handles = []
    scope = guardian = target = outsider = None
    def record(name, kind='actual_infrastructure'):
        report['checks'].append({'name': name, 'kind': kind})
        print('GUARDIAN_PASS ' + name, flush=True)
    async def verify_dead(birth):
        async with asyncio.timeout(15):
            while True:
                observation = identity.probe(birth)
                if observation['state'] == 'exited':
                    return observation
                await asyncio.sleep(.02)
    try:
        outsider = subprocess.Popen([BASE, '-I', '-S', '-c', 'import time; time.sleep(300)'],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        outside_handle = ProcessHandle(int(outsider._handle)); handles.append(outside_handle)
        outside_birth = outside_handle.identity()
        normal = root / 'normal'; normal.mkdir()
        scope = WindowsScope(uuid.uuid4().hex * 2, observe=False)
        guardian = Guardian(scope, normal / 'guardian')
        guardian.start()
        target = await creation.create(scope, BASE, '-X', 'utf8', '-B', str(ENTRY), '--payload', str(normal))
        child_birth = await await_file(normal / 'payload.json')
        leaf_birth = await await_file(normal / 'leaf.json')
        for birth in (child_birth, leaf_birth):
            process = ProcessHandle.query(birth); handles.append(process)
            assert scope.contains(process)
        proof = await asyncio.to_thread(guardian.retire)
        report['receipts'].append(proof)
        # Windows/runtime helpers may also join the Job. Every observed member
        # must exit; the known root and leaf are independently checked below.
        assert proof['receipt']['membership']['total'] >= 2
        assert await asyncio.wait_for(target.wait(), 10) != 0
        for process in handles[1:]:
            process.wait_exit(0)
        assert outside_handle.identity() == outside_birth
        record('actual guardian seals and verifies root plus descendant while preserving an outside process')
        guardian.close(); guardian = None
        scope.close(); scope = None

        for mode in ('empty', 'creation_gap', 'tree'):
            current = root / mode; current.mkdir()
            api = subprocess.Popen([BASE, '-X', 'utf8', '-B', str(ENTRY), '--owner', str(current), mode],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            api_handle = ProcessHandle(int(api._handle)); handles.append(api_handle)
            api_birth = api_handle.identity()
            assert await asyncio.to_thread(api.wait, 25) == 81
            api_handle.wait_exit(0)
            armed = await await_file(current / 'armed.json')
            assert armed['request']['owner'] == api_birth
            receipt = await await_file(current / 'guardian/receipt.json')
            protocol.validate_receipt(armed['request'], armed['prepared'], receipt)
            exited = await verify_dead(armed['prepared']['guardian'])
            assert receipt['reason'] in {'owner_exited', 'owner_pipe_closed'}
            if mode == 'empty':
                assert receipt['membership']['total'] == 0
            else:
                assert receipt['membership']['total'] >= {'creation_gap': 1, 'tree': 2}[mode]
            births = []
            if mode == 'creation_gap':
                births.append(json.loads((current / 'created-before-adoption.json').read_text()))
            elif mode == 'tree':
                births.extend(json.loads((current / name).read_text()) for name in ('payload.json', 'leaf.json'))
            for birth in births:
                await verify_dead(birth)
            report['receipts'].append({'request': armed['request'], 'preparation': armed['prepared'],
                'receipt': receipt, 'guardian_exit': exited, 'owner_exit': identity.probe(api_birth), 'actual_business_births': births})
            assert outside_handle.identity() == outside_birth
            record('actual API exit at ' + mode + ' retains Job proof and verifies every business member')

        deadline = root / 'deadline'; deadline.mkdir()
        scope = WindowsScope(uuid.uuid4().hex * 2, observe=False)
        guardian = Guardian(scope, deadline / 'guardian', max_seconds=2)
        guardian.start()
        target = await creation.create(scope, BASE, '-I', '-S', '-c', 'import time; time.sleep(300)')
        proof = await asyncio.to_thread(guardian.wait, 12)
        assert proof['receipt']['reason'] == 'deadline' and proof['receipt']['membership']['total'] >= 1
        assert await asyncio.wait_for(target.wait(), 10) != 0
        report['receipts'].append(proof)
        record('actual guardian deadline seals and drains the scope with the owner still alive')
        guardian.close(); guardian = None
        scope.close(); scope = None

        failed = root / 'guardian_failure'; failed.mkdir()
        scope = WindowsScope(uuid.uuid4().hex * 2, observe=False)
        guardian = Guardian(scope, failed / 'guardian')
        guardian.start()
        target = await creation.create(scope, BASE, '-I', '-S', '-c', 'import time; time.sleep(300)')
        guardian.proc.kill()  # Retained Popen creation handle, never PID lookup.
        try:
            await asyncio.to_thread(guardian.wait, 10)
        except ValueError:
            pass
        else:
            raise AssertionError('Killed guardian was accepted as complete')
        assert not (failed / 'guardian/receipt.json').exists()
        assert scope.active_processes() >= 1 and target.returncode is None
        scope.terminate()
        await asyncio.to_thread(scope.wait_empty)
        assert await asyncio.wait_for(target.wait(), 10) != 0
        record('actual guardian death gives no closure receipt and does not falsely declare remaining member exited')
        guardian.close(); guardian = None
        scope.close(); scope = None

        original = report['receipts'][0]
        for change in ('pending_member', 'foreign_guardian', 'foreign_request'):
            altered = copy.deepcopy(original['receipt'])
            if change == 'pending_member': altered['membership']['pending'] = 1
            elif change == 'foreign_guardian': altered['guardian']['creation_time_100ns'] = '1'
            else: altered['request_sha256'] = '0' * 64
            try:
                protocol.validate_receipt(original['request'], original['preparation'], altered)
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid guardian proof was accepted')
        record('pending member and changed guardian or request bindings are rejected', 'protocol_fault')
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'detail': str(error)[:1000]}
    finally:
        try:
            if guardian is not None: await asyncio.to_thread(guardian.close)
            if scope is not None:
                scope.terminate(); await asyncio.to_thread(scope.wait_empty); scope.close()
            if target is not None: await asyncio.wait_for(target.wait(), 10)
            if outsider is not None:
                outsider.terminate(); await asyncio.to_thread(outsider.wait, 10)
            for handle in handles:
                handle.wait_exit(0); handle.close()
        except BaseException as error:
            report['cleanup_errors'].append(type(error).__name__)
        report['passed'] = report['passed'] and not report['cleanup_errors']
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (root / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps({'passed': report['passed'], 'evidence': str(root / 'result.json')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.path.insert(0, str(ENTRY.parents[1] / 'tools'))
    from regression_llm_policy import enforce
    enforce([ENTRY])
    if len(sys.argv) >= 3 and sys.argv[1] in {'--payload', '--leaf'}:
        payload(Path(sys.argv[2]), sys.argv[1] == '--leaf')
    elif len(sys.argv) == 4 and sys.argv[1] == '--owner':
        asyncio.run(owner(Path(sys.argv[2]), sys.argv[3]))
    else:
        root = ENTRY.parents[2] / 'tmp' / ('windows-scope-guard-' + uuid.uuid4().hex)
        root.mkdir(exist_ok=False)
        raise SystemExit(asyncio.run(verify(root)))
