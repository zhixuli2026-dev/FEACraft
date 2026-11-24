"""Real exited-process cache recovery and preservation boundaries; no DB writes."""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SERVER = Path(__file__).resolve().parent


def child(kind):
    from app.services.private_cache import Cache
    cache = Cache(kind)
    cache.write('nested/input.txt', b'original')
    cache.write('empty.txt', b'')
    print(cache.container, flush=True)
    sys.stdin.read()


def main():
    report = Path(tempfile.mkdtemp(prefix='private-cache-recovery-', dir=SERVER.parents[1] / 'tmp'))
    os.environ['VIBEFLOW_STORAGE_FS_ROOT'] = str(report / 'cache')
    from app.config import get_settings
    get_settings.cache_clear()
    from app.services import private_cache as caches
    from app.services.fs_directory import remove_owned
    checks = []
    modes = ['normal', 'changed', 'foreign-file', 'foreign-directory', 'partial',
             'host', 'birth', 'invalid-owner', 'truncated-journal', 'missing-owner', 'hardlink']
    for kind in sorted(caches.KINDS):
        for mode in modes:
            process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', __file__, '--child', kind],
                                       cwd=SERVER, env=os.environ.copy(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf8')
            container = None
            try:
                line = process.stdout.readline().strip()
                assert line, process.stderr.read()
                container = Path(line)
                row = next(r for r in caches.recover() if r['name'] == container.name)
                assert row['status'] == 'active', row
                assert (container / 'data/nested/input.txt').read_bytes() == b'original'
                # Windows venv python.exe can be a launcher: kill the recorded
                # interpreter, not just its launcher process.
                actual_pid = json.loads((container / 'owner.json').read_text())['owner']['pid']
                os.kill(actual_pid, signal.SIGTERM)
                process.wait(timeout=10)
                owner = container / 'owner.json'
                if mode == 'changed':
                    (container / 'data/nested/input.txt').write_bytes(b'changed!')
                elif mode == 'partial':
                    (container / 'data/nested/input.txt').write_bytes(b'ori')
                elif mode == 'foreign-file':
                    (container / 'data/keep.txt').write_bytes(b'foreign')
                elif mode == 'foreign-directory':
                    (container / 'data/keep').mkdir()
                elif mode in {'host', 'birth'}:
                    value = json.loads(owner.read_text())
                    if mode == 'host':
                        value['owner']['host_id'] = '0' * 64
                    else:
                        # A real live PID with a different birth marker must not
                        # be treated as the exited owner, even on PID reuse.
                        from app.services import process_identity_native as identity
                        value['owner'] = identity.observe(os.getpid())
                        key = 'creation_time_100ns' if os.name == 'nt' else 'start_ticks'
                        value['owner'][key] = str(int(value['owner'][key]) + 1)
                    owner.write_text(json.dumps(value))
                elif mode == 'invalid-owner':
                    owner.write_text('{}')
                elif mode == 'truncated-journal':
                    with (container / 'files.jsonl').open('ab') as stream:
                        stream.write(b'{')
                elif mode == 'missing-owner':
                    owner.unlink()
                elif mode == 'hardlink':
                    os.link(container / 'data/nested/input.txt', report / 'outside-link')
                before = {p.relative_to(container).as_posix(): p.read_bytes()
                          for p in container.rglob('*') if p.is_file()}
                row = next(r for r in caches.recover() if r['name'] == container.name)
                if mode == 'normal':
                    assert row['status'] == 'removed' and not container.exists(), row
                    assert not any(r.get('name') == container.name for r in caches.recover())
                else:
                    assert row['status'] in {'recovery_required', 'unverified_retained'}, (kind, mode, row)
                    after = {p.relative_to(container).as_posix(): p.read_bytes()
                             for p in container.rglob('*') if p.is_file()}
                    assert before == after, (kind, mode)
                checks.append(kind + ':' + mode)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                (report / 'outside-link').unlink(missing_ok=True)
                if container and container.exists():
                    remove_owned(container, root=report)
        legacy = caches.parent_for(kind) / 'read-legacy'
        legacy.mkdir()
        (legacy / 'keep').write_bytes(b'legacy')
        row = next(r for r in caches.recover() if r['name'] == legacy.name)
        assert row['status'] == 'untracked_retained' and (legacy / 'keep').read_bytes() == b'legacy'
        checks.append(kind + ':legacy-retained')
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(report)}
    (report / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result), flush=True)


def automatic():
    from testsupport import IsolatedServer
    api = IsolatedServer('private-cache-automatic', app='private_cache_fixture:app')
    (api.root / 'private_cache_fixture.py').write_text(
        "import os\nos.environ['VIBEFLOW_STORAGE_MAINTENANCE_ENABLED']='true'\n"
        "os.environ['VIBEFLOW_STORAGE_MAINTENANCE_INTERVAL_SECONDS']='1'\n"
        "from app.main import app\n", encoding='utf8')
    children, containers, checks = [], [], []
    env = dict(os.environ, VIBEFLOW_STORAGE_FS_ROOT=str(api.root / 'files'))
    try:
        for kind in ('.attempt-sources', '.case-read-views'):
            process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', __file__, '--child', kind],
                                       cwd=SERVER, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf8')
            children.append(process)
            line = process.stdout.readline().strip()
            assert line, process.stderr.read()
            containers.append(Path(line))
        first_owner = json.loads((containers[0] / 'owner.json').read_text())['owner']['pid']
        os.kill(first_owner, signal.SIGTERM)
        children[0].wait(timeout=10)
        with api:
            deadline = time.monotonic() + 15
            while containers[0].exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert not containers[0].exists()
            checks.append('startup_removes_exited_source_cache_without_registered_users')
            assert (containers[1] / 'data/nested/input.txt').read_bytes() == b'original'
            checks.append('startup_preserves_live_read_view')
            second_owner = json.loads((containers[1] / 'owner.json').read_text())['owner']['pid']
            os.kill(second_owner, signal.SIGTERM)
            children[1].wait(timeout=10)
            deadline = time.monotonic() + 15
            while containers[1].exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert not containers[1].exists()
            checks.append('periodic_scan_removes_newly_exited_read_view')
    finally:
        for process in children:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(api.root)}
    (api.root / 'automatic-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    if '--automatic' in sys.argv:
        automatic()
    elif '--child' in sys.argv:
        child(sys.argv[2])
    else:
        main()
