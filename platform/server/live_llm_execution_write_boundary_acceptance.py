"""Observe genuine cmd, WSL and sandbox write boundaries using disposable files only."""

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
import errno
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

import aiohttp

from app.services.owned_process import wsl_path
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from terminal_execution_smoke import connect, exited
from testsupport import IsolatedServer, SERVER


DISTRIBUTION = 'vibeflow-linux'
MIB = 1024 * 1024
MARKER = 'rb17_marker.bin'
RESULT = re.compile(rb'VF_RB17_REPORT:(\{[^\r\n]+\})')

FIXTURE = '''
from fastapi import Depends
from sqlalchemy import select
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.models import DurableEvent, JobAttempt
from app.routers.jobs import _get_job
get_settings().execution_workspace_limit_mb = 1

@app.get('/api/v1/testing/rb17/usage/{job_id}')
async def usage(job_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    job = await _get_job(job_id, user, db, write=False)
    rows = await db.scalars(select(DurableEvent).join(JobAttempt, JobAttempt.id == DurableEvent.attempt_id)
        .where(JobAttempt.job_id == job.id, DurableEvent.case_id == job.case_id,
          DurableEvent.type.in_(['attempt.resource_usage', 'attempt.storage_usage'])).order_by(DurableEvent.seq))
    return {row.type: json.loads(row.payload_json) for row in rows}
'''

PROBE = '''
import json
import os
from pathlib import Path
import stat
import sys
import time

mode = sys.argv[1]
external = {
    'windows-c': Path(sys.argv[2]),
    'windows-d': Path(sys.argv[3]),
}
if sys.platform == 'linux':
    external['linux-temp'] = Path(sys.argv[4])
paths = dict(external, workdir=Path.cwd())
observed = []
if mode in {'sandbox-terminal', 'sandbox-job', 'sandbox-job-pressure'}:
    paths.update({'case': Path('/case/Ref'), 'sandbox-tmp': Path('/tmp'), 'sandbox-output': Path('/output')})
    observed = ['/', '/case', '/tmp', '/output']
elif sys.platform == 'linux':
    observed = [str(external['windows-c']), str(external['windows-d']), str(external['linux-temp'])]

def write_probe(path):
    marker = path / 'rb17_marker.bin'
    data = (mode + '\\n').encode() * 17
    try:
        with marker.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        assert marker.read_bytes() == data
        if path not in external.values():
            marker.unlink()
    except OSError as error:
        return {'status': 'denied', 'errno': error.errno}
    return {'status': 'written', 'bytes': len(data)}

def mount_probe(path):
    candidate = Path(path)
    info = candidate.stat()
    fs = os.statvfs(candidate)
    source = {}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        prefix, suffix = line.split(' - ', 1)
        mount = prefix.split()[4]
        if candidate == Path(mount) or Path(mount) in candidate.parents:
            if len(mount) >= len(source.get('mount', '')):
                source = {'mount': mount, 'fs': suffix.split()[0]}
    return {**source, 'device': info.st_dev, 'mode': stat.S_IMODE(info.st_mode),
            'bytes_total': fs.f_blocks * fs.f_frsize, 'bytes_available': fs.f_bavail * fs.f_frsize}

result = {'uid': os.geteuid() if sys.platform == 'linux' else None,
          'writes': {name: write_probe(path) for name, path in paths.items()},
          'mounts': {name: mount_probe(name) for name in observed}}
print('VF_RB17_REPORT:' + json.dumps(result, separators=(',', ':')), flush=True)
if mode == 'sandbox-job-pressure':
    # A 1.25 MiB file inside this job's disposable output bind crosses the
    # configured 1 MiB directory limit while the command still owns its scope.
    with Path('/output/rb17-pressure.bin').open('xb') as stream:
        for i in range(20):
            stream.write(b'Q' * 65536)
        stream.flush()
        os.fsync(stream.fileno())
    print('VF_RB17_OUTPUT_BYTES:' + str(Path('/output/rb17-pressure.bin').stat().st_size), flush=True)
    time.sleep(30)
'''


def linux(*args):
    return subprocess.run(['wsl.exe', '-d', DISTRIBUTION, '--user', 'nobody', '--exec', *args],
                          check=True, capture_output=True, timeout=14).stdout


@contextmanager
def controlled_paths(root):
    temp = Path(tempfile.gettempdir()).resolve(strict=True)
    assert temp.drive.casefold() == 'c:' and root.resolve(strict=True).drive.casefold() == 'd:'
    c = Path(tempfile.mkdtemp(prefix='vf-rb17-', dir=temp))
    d = root / 'test-only-drive-d'
    d.mkdir()
    native = None
    try:
        path = linux('mktemp', '-d', '-p', '/tmp', 'vf-rb17-XXXXXXXX').decode().strip()
        assert re.fullmatch(r'/tmp/vf-rb17-[a-zA-Z0-9]{8}', path)
        native = path
        yield c, d, native
    finally:
        for candidate in (c, d):
            assert candidate.resolve(strict=True).parent == candidate.parent.resolve(strict=True)
            (candidate / MARKER).unlink(missing_ok=True)
            candidate.rmdir()
        if native is not None:
            linux('rm', '-f', '--', native + '/' + MARKER)
            linux('rmdir', '--', native)


def upload(api):
    api.call('PUT', api.prefix + '/file', {'path': 'Ref/rb17-probe.py', 'content': PROBE})


async def terminal_read(ws, *, phase):
    output = b''
    async with asyncio.timeout(55):
        while True:
            frame = await ws.receive()
            if frame.type == aiohttp.WSMsgType.BINARY:
                output += frame.data
                if phase == 'report' and (match := RESULT.search(output)):
                    return json.loads(match[1])
            elif frame.type == aiohttp.WSMsgType.TEXT:
                value = json.loads(frame.data)
                if phase == 'exit' and value == {'type': 'exit', 'code': 0}:
                    return value
                raise AssertionError(value)
            else:
                raise AssertionError(frame.type)


async def run_terminal(api, client, shell, argument, paths):
    c, d, native = paths
    api.create_case('RB-17 ' + argument)
    upload(api)
    identifier = None
    ws = await connect(client, api, shell)
    try:
        async with asyncio.timeout(100):
            frame = await ws.receive()
        assert frame.type == aiohttp.WSMsgType.TEXT, frame.type
        status = json.loads(frame.data)
        assert status['type'] == 'ready', status
        identifier = status['session']
        args = [argument, str(c), str(d), native] if shell == 'cmd' else [argument, wsl_path(c), wsl_path(d), native]
        command = (subprocess.list2cmdline([sys._base_executable, '-I', '-S', '-u', r'Ref\rb17-probe.py', *args]) + '\r\n'
                   if shell == 'cmd' else shlex.join(['python3', '-I', '-S', '-u', 'Ref/rb17-probe.py', *args]) + '\n')
        await ws.send_json({'type': 'input', 'data': command})
        result = await terminal_read(ws, phase='report')
        await ws.send_json({'type': 'input', 'data': 'exit\r\n' if shell == 'cmd' else 'exit\n'})
        await terminal_read(ws, phase='exit')
        row = api.call('GET', '/terminal/sessions/' + identifier)
        assert row['phase'] == 'closed' and row['cleanup_verified'] is True
        await exited(row)
        result.update(session=identifier, stop=row['stop'], cleanup_verified=True)
        return result
    finally:
        await ws.close()
        if identifier and api.call('GET', '/terminal/sessions/' + identifier)['phase'] not in {'failed', 'closed'}:
            api.call('DELETE', '/terminal/sessions/' + identifier, expect=(200, 202, 409))


def run_job(api, argument, paths):
    c, d, native = paths
    api.create_case('RB-17 ' + argument)
    upload(api)
    command = shlex.join(['python3', '-I', '-S', '-u', 'Ref/rb17-probe.py', argument, wsl_path(c), wsl_path(d), native])
    request = {'runtime': 'wsl-sandbox', 'command': command, 'input_paths': ['Ref/rb17-probe.py'],
               'timeout_seconds': 50, 'max_log_bytes': 65536}
    job_id = api.call('POST', api.prefix + '/jobs', request, expect=(200, 202),
                      extra_headers={'Idempotency-Key': uuid.uuid4().hex})['id']
    failed = argument == 'sandbox-job-pressure'
    result = api.wait_job(job_id, timeout=80, expected='failed' if failed else 'succeeded')
    stop = result['attempt']['process_stop']
    assert stop['verified'] and stop['scope_empty']
    usage = api.call('GET', f'/testing/rb17/usage/{job_id}')
    output = api.call('GET', f'/jobs/{job_id}/log?offset=0')['chunk']
    assert (match := RESULT.search(output.encode())), output
    measured = json.loads(match[1])
    if failed:
        assert 'VF_RB17_OUTPUT_BYTES:1310720' in output
        assert result['attempt']['reason_code'] == 'resource_limit' and stop['reason'] == 'disk_limit'
        assert usage['attempt.resource_usage']['violation'] == 'disk_limit'
        assert usage['attempt.storage_usage']['peak_bytes'] >= 1310720 > MIB
    measured.update(job_id=job_id, status=result['status'], stop=stop, usage=usage)
    return measured


def assert_sandbox(result, *, legacy):
    assert result['uid'] == 65534, result
    assert all(result['writes'][name] == {'status': 'denied', 'errno': errno.ENOENT}
               for name in ('windows-c', 'windows-d', 'linux-temp')), result
    assert result['writes']['case']['status'] == result['writes']['sandbox-tmp']['status'] == 'written', result
    mounts = result['mounts']
    assert mounts['/']['fs'] == 'tmpfs' and mounts['/']['bytes_total'] == 512 * MIB, mounts
    assert mounts['/tmp']['fs'] == 'tmpfs' and mounts['/tmp']['bytes_total'] == 128 * MIB, mounts
    if legacy:
        assert mounts['/case']['device'] == mounts['/tmp']['device'], mounts
        assert mounts['/output']['device'] != mounts['/']['device'] and mounts['/output']['fs'] != 'tmpfs', mounts
        assert result['writes']['sandbox-output']['status'] == 'written', result
    else:
        assert mounts['/case']['fs'] == 'ext4' and 32 * MIB < mounts['/case']['bytes_total'] <= 128 * MIB, mounts
        assert mounts['/case']['device'] != mounts['/tmp']['device'], mounts
        assert mounts['/output']['device'] == mounts['/']['device'], mounts


async def verify(database_url, store):
    api = IsolatedServer('execution-write-boundary', app='write_boundary_fixture:app', database_url=database_url)
    settings = {'VIBEFLOW_TERMINAL_SESSION_LIFETIME_SECONDS': '0', 'VIBEFLOW_TERMINAL_SESSION_MAX_OUTPUT_BYTES': '0'}
    if store is not None:
        settings.update(store.settings())
    (api.root / 'write_boundary_fixture.py').write_text('import json, os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, 'utf8')
    report = {'passed': False, 'database': 'postgresql' if database_url else 'sqlite',
              'storage': 's3' if store else 'fs', 'real_model_requests': 0, 'mock_llm_calls': 0, 'probes': {}}
    probes = report['probes']
    try:
        with controlled_paths(api.root) as paths, api:
            c, d, native = paths
            async with aiohttp.ClientSession() as client:
                probes['ordinary-cmd'] = await run_terminal(api, client, 'cmd', 'ordinary-cmd', paths)
                for name, directory in [('windows-c', c), ('windows-d', d)]:
                    assert probes['ordinary-cmd']['writes'][name]['status'] == 'written'
                    assert (directory / MARKER).read_bytes() == b'ordinary-cmd\n' * 17
                    (directory / MARKER).unlink()
                probes['ordinary-wsl'] = await run_terminal(api, client, 'wsl', 'ordinary-wsl', paths)
                assert probes['ordinary-wsl']['uid'] == 65534
                for directory in (c, d):
                    assert (directory / MARKER).read_bytes() == b'ordinary-wsl\n' * 17
                    (directory / MARKER).unlink()
                assert linux('cat', '--', native + '/' + MARKER) == b'ordinary-wsl\n' * 17
                linux('rm', '--', native + '/' + MARKER)
                probes['sandbox-terminal'] = await run_terminal(api, client, 'wsl-sandbox', 'sandbox-terminal', paths)
                assert_sandbox(probes['sandbox-terminal'], legacy=False)
            for mode in ('sandbox-job', 'sandbox-job-pressure'):
                probes[mode] = await asyncio.to_thread(run_job, api, mode, paths)
                assert_sandbox(probes[mode], legacy=True)
            assert not (c / MARKER).exists() and not (d / MARKER).exists()
        report['passed'] = report['api_exited'] = api.proc.poll() is not None
    finally:
        (api.root / 'write-boundary.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), 'utf8')
    return api.root


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    args = parser.parse_args()
    store = (ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed', volume_max=6)
             if args.s3 else nullcontext(None))
    async with disposable_database(args.postgres) as url:
        with store as selected:
            root = await verify(url, selected)
        if args.s3:
            assert selected.proc.poll() is not None
            (root / 's3-cleanup.json').write_text(json.dumps({'stopped': True, 'root': str(selected.root)}), 'utf8')
    print('EXECUTION_WRITE_BOUNDARY_OK', root, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
