"""Exercise the formal recovery ZIP endpoint with a real WSL recovery and HTTP clients."""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
import uuid

import aiohttp

from testsupport import IsolatedServer


FIXTURE = r'''
import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

from fastapi import Depends
from app.archive_admission import ArchiveAdmission
from app.deps import get_current_user
from app.main import app
from app.services import process_identity_native, request_deadline

root = Path(__file__).parent
observed = ContextVar('recovery_download_test', default=None)
original_open = Path.open
original_seconds = ArchiveAdmission.seconds
short_seconds = 4


class FixtureTime:
    def __get__(self, instance, owner):
        state = observed.get()
        if state is not None and state['mode'] in {'expiry', 'send-expiry'}:
            return short_seconds
        return original_seconds


ArchiveAdmission.seconds = FixtureTime()


def record(name, value):
    target = root / name
    temporary = target.with_suffix('.writing')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(target)


class ArchiveFile:
    def __init__(self, file, state):
        self.file = file
        self.state = state
        self.reading_body = False

    def __getattr__(self, name):
        return getattr(self.file, name)

    def seek(self, offset, whence=0):
        result = self.file.seek(offset, whence)
        if offset == whence == 0:
            self.reading_body = True
        return result

    def read(self, amount):
        state = self.state
        if self.reading_body:
            deadline = request_deadline.current()
            started = time.monotonic()
            data = self.file.read(amount)
            state['body_read_windows'].append({'started':started, 'returned':time.monotonic(),
                                               'deadline':deadline.deadline if deadline is not None else None})
            state['body_reads'] += 1
            state['body_bytes'] += len(data)
            return data
        data = self.file.read(amount)
        state['verify_reads'] += 1
        state['verify_bytes'] += len(data)
        if state['verify_reads'] == 1:
            deadline = request_deadline.current()
            state['verify_inherited_deadline'] = deadline is not None
            if state['mode'] == 'held':
                record('held-first.json', state)
                until = time.monotonic() + 16
                while not (root / 'release-held').exists():
                    if time.monotonic() >= until:
                        raise TimeoutError('Controlled real ZIP read was not released')
                    time.sleep(.02)
            elif state['mode'] == 'expiry':
                record('expiry-first.json', state)
                time.sleep((deadline.remaining() if deadline is not None else short_seconds) + .15)
                if deadline is not None:
                    record('expiry-crossed.json', {'deadline_expired':deadline.expired, 'state':state})
                    until = time.monotonic() + 12
                    while not (root / 'release-expiry').exists():
                        if time.monotonic() >= until:
                            raise TimeoutError('Controlled expired ZIP read was not released')
                        time.sleep(.02)
        return data

    def close(self):
        self.file.close()
        self.state['closed'] = self.file.closed


def observe_open(self, *args, **kwargs):
    file = original_open(self, *args, **kwargs)
    state = observed.get()
    if (state is not None and self.name == 'recovery.zip' and self.parent.parent.name == '.terminal-recovery'
            and self.is_relative_to(root)):
        state['opens'] += 1
        return ArchiveFile(file, state)
    return file


Path.open = observe_open


@app.get('/fixture/recovery-download-owner')
def owner(user=Depends(get_current_user)):
    return process_identity_native.observe(os.getpid())


class ObserveDownload:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        mode = dict(scope.get('headers', ())).get(b'x-fixture-download', b'').decode('ascii')
        if (not scope.get('path', '').startswith('/api/v1/terminal/sessions/')
                or not scope['path'].endswith('/download')
                or mode not in {'held', 'parallel', 'expiry', 'expiry-contender', 'disconnect', 'send-expiry', 'reopen'}):
            return await self.app(scope, receive, send)
        state = {'mode':mode, 'opens':0, 'verify_reads':0, 'verify_bytes':0, 'body_reads':0,
                 'body_bytes':0, 'body_read_windows':[], 'send_blocks':0, 'closed':False, 'disconnect':False}
        token = observed.set(state)

        async def watched_receive():
            message = await receive()
            if message['type'] == 'http.disconnect':
                state['disconnect'] = True
            return message

        async def watched_send(message):
            await send(message)
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
            if message['type'] == 'http.response.body' and message.get('body'):
                state['send_blocks'] += 1
                if state['send_blocks'] == 1 and mode in {'disconnect', 'send-expiry'}:
                    record(mode + '-first.json', state)
                    if mode == 'send-expiry':
                        deadline = request_deadline.current()
                        await asyncio.sleep((deadline.remaining() if deadline else short_seconds) + .15)
                    else:
                        until = time.monotonic() + 12
                        while not state['disconnect'] and not (root / 'release-disconnect').exists():
                            if time.monotonic() >= until:
                                raise TimeoutError('Disconnect observation was not released')
                            await asyncio.sleep(.025)

        try:
            await self.app(scope, watched_receive, watched_send)
        finally:
            observed.reset(token)
            record(mode + '-trace.json', state)


app = ObserveDownload(app)
'''


async def call(api, method, path, body=None, **options):
    return await asyncio.to_thread(api.call, method, path, body, **options)


async def wait_report(api, name, timeout=20):
    path = api.root / name
    async with asyncio.timeout(timeout):
        while not path.is_file():
            await asyncio.sleep(.03)
    return json.loads(path.read_text(encoding='utf-8'))


async def genuine_recovery(api, report):
    await asyncio.to_thread(api.create_case, 'Owned recovery ZIP admission')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=330)) as client:
        async with client.ws_connect(api.base + '/ws/terminal', params={
                'token':api.token, 'case':api.case_id, 'shell':'wsl-sandbox'}) as ws:
            ready = await ws.receive_json(timeout=110)
            assert ready['type'] == 'ready', ready
            session = ready['session']
            await ws.send_json({'type':'input', 'data':
                'dd if=/dev/urandom of=Ref/recovery-download.bin bs=1048576 count=6 status=none; '
                'sha256sum Ref/recovery-download.bin\r'})
            output = b''
            async with asyncio.timeout(35):
                match = None
                while match is None:
                    packet = await ws.receive()
                    assert packet.type == aiohttp.WSMsgType.BINARY, packet
                    output += packet.data
                    assert len(output) <= 65536
                    match = re.search(rb'([a-f0-9]{64})  Ref/recovery-download\.bin', output)
            file_digest = match.group(1).decode('ascii')
            report['before_restart'] = await call(api, 'GET', '/fixture/recovery-download-owner', api_prefix=False)
            args = api.proc.args
            api.proc.kill()
            report['original_api_returncode'] = await asyncio.to_thread(api.proc.wait, 12)

        api.proc = subprocess.Popen(args, cwd=api.root, env=api.env, stdout=api.log,
                                   stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        async with asyncio.timeout(45):
            while True:
                assert api.proc.poll() is None, 'Restarted isolated API exited'
                try:
                    if (await call(api, 'GET', '/api/health', api_prefix=False)).get('ok'):
                        break
                except OSError:
                    pass
                await asyncio.sleep(.12)

        report['after_restart'] = await call(api, 'GET', '/fixture/recovery-download-owner', api_prefix=False)
        base = '/terminal/sessions/' + session
        async with asyncio.timeout(75):
            while True:
                proof = await call(api, 'GET', base + '/recovery-inspection')
                if proof['status'] == 'processes_exited':
                    break
                assert proof['reason'] in {'process_still_active', 'process_exit_unverified', 'inspection_timeout'}, proof
                await asyncio.sleep(.2)
        assert proof['checks'] and all(item['state'] == 'exited' for item in proof['checks']), proof
        report['original_process_stops'] = proof['checks']
        async with client.post(api.base + '/api/v1' + base + '/recoveries',
                               headers={'Authorization':'Bearer ' + api.token}) as response:
            recovery = await response.json()
            assert response.status == 201 and recovery['phase'] == 'succeeded', (response.status, recovery)
        assert recovery['cleanup_verified'] is True
        entry = recovery['manifest']['files']['Ref/recovery-download.bin']
        assert entry['size'] == 6*1024*1024 and entry['sha256'] == file_digest, entry
        report['recovery_receipt'] = recovery['receipt']
        return api.base + '/api/v1' + base + '/recoveries/' + recovery['id'] + '/download'


async def get(client, endpoint, auth, mode):
    headers = {**auth, 'X-Fixture-Download': mode}
    async with client.get(endpoint, headers=headers) as response:
        if response.status != 200:
            detail = (await response.json())['detail']
            return {'status':response.status, 'code':detail['code'], 'retry':response.headers.get('Retry-After')}
        digest, size = hashlib.sha256(), 0
        async for block in response.content.iter_chunked(65536):
            digest.update(block)
            size += len(block)
        assert size == int(response.headers['Content-Length'])
        assert response.headers['Content-Type'] == 'application/zip'
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert response.headers['Content-Disposition'].startswith('attachment; filename="sandbox-recovery-')
        return {'status':200, 'sha256':digest.hexdigest(), 'size':size}


def assert_stopped_send(trace):
    # BaseHTTPMiddleware can hand one chunk to the outer sender while the
    # inner iterator prepares the next chunk for its zero-buffer channel.
    assert trace['send_blocks'] == 1 and trace['closed'], trace
    assert trace['body_reads'] == len(trace['body_read_windows']) in {1, 2}, trace
    assert all(window['deadline'] is not None and window['started'] < window['deadline']
               and window['started'] <= window['returned'] for window in trace['body_read_windows']), trace


async def verify(api, report, baseline):
    endpoint = await genuine_recovery(api, report)
    auth = {'Authorization':'Bearer ' + api.token}
    receipt = report['recovery_receipt']
    original = {'status':200, 'sha256':receipt['archive_sha256'], 'size':receipt['archive_bytes']}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as client:
        held = asyncio.create_task(get(client, endpoint, auth, 'held'))
        try:
            first = await wait_report(api, 'held-first.json')
            assert first['verify_reads'] == 1 and first['verify_bytes'] > 0
            parallel = report['parallel'] = await get(client, endpoint, auth, 'parallel')
        finally:
            (api.root / 'release-held').touch()
            assert await held == original
        parallel_trace = report['parallel_trace'] = await wait_report(api, 'parallel-trace.json')
        if baseline:
            assert parallel == original and parallel_trace['opens'] == 1, parallel_trace
        else:
            assert parallel == {'status':429, 'code':'archive_busy', 'retry':'2'}, parallel
            assert parallel_trace['opens'] == 0, parallel_trace

        started = time.monotonic()
        if baseline:
            expired = await get(client, endpoint, auth, 'expiry')
        else:
            expiring = asyncio.create_task(get(client, endpoint, auth, 'expiry'))
            try:
                crossed = report['expiry_while_draining'] = await wait_report(api, 'expiry-crossed.json')
                assert crossed['deadline_expired'] and crossed['state']['verify_reads'] == 1 and not expiring.done()
                contender = report['expiry_contender'] = await get(client, endpoint, auth, 'expiry-contender')
                assert contender == {'status':429, 'code':'archive_busy', 'retry':'2'}, contender
                challenger = await wait_report(api, 'expiry-contender-trace.json')
                assert challenger['opens'] == 0 and not expiring.done(), challenger
            finally:
                (api.root / 'release-expiry').touch()
                expired = await expiring
        report['expired'] = expired
        report['expiry_elapsed_seconds'] = round(time.monotonic() - started, 4)
        trace = report['expiry_trace'] = await wait_report(api, 'expiry-trace.json')
        assert trace['opens'] == 1 and trace['closed'], trace
        if baseline:
            assert expired == original and trace['verify_reads'] > 1 and not trace['verify_inherited_deadline'], trace
        else:
            assert expired['status'] == 504 and expired['code'] == 'archive_timeout', expired
            assert trace['verify_reads'] == 1 and trace['verify_inherited_deadline'] and trace['body_reads'] == 0, trace

        if not baseline:
            interrupted = False
            try:
                async with client.get(endpoint, headers={**auth,'X-Fixture-Download':'send-expiry'}) as response:
                    assert response.status == 200
                    assert await response.content.readexactly(32768)
                    await response.read()
            except (aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError):
                interrupted = True
            send_trace = report['send_expiry_trace'] = await wait_report(api, 'send-expiry-trace.json')
            assert interrupted, send_trace
            assert_stopped_send(send_trace)

            try:
                async with client.get(endpoint, headers={**auth,'X-Fixture-Download':'disconnect'}) as response:
                    assert response.status == 200
                    assert await response.content.readexactly(32768)
                    await wait_report(api, 'disconnect-first.json')
                    response.close()
                disconnected = report['disconnect_trace'] = await wait_report(api, 'disconnect-trace.json', timeout=10)
            finally:
                (api.root / 'release-disconnect').touch()
            assert disconnected['disconnect'], disconnected
            assert_stopped_send(disconnected)

        assert await get(client, endpoint, auth, 'reopen') == original
        report['reopen_trace'] = await wait_report(api, 'reopen-trace.json')
        async with client.get(endpoint) as denied:
            assert denied.status == 401
        stranger = await call(api, 'POST', '/auth/register', {
            'email':uuid.uuid4().hex+'@example.com', 'name':'Other archive owner', 'password':'isolated-test-123'})
        async with client.get(endpoint, headers={'Authorization':'Bearer ' + stranger['token']}) as denied:
            assert denied.status == 404
    assert report['reopen_trace']['closed']


def run():
    baseline = os.environ.get('VIBEFLOW_RECOVERY_DOWNLOAD_BASELINE') == '1'
    api = IsolatedServer('recovery-download-admission', app='observed_recovery_download:app')
    result = {'passed':False,'baseline':baseline, 'real_model_calls':0, 'mock_llm_calls':0,'api_port':api.port}
    print('RECOVERY_DOWNLOAD_ADMISSION ' + str(api.root), flush=True)
    try:
        scratch = api.root / 'native-temp'
        scratch.mkdir()
        settings = {name: str(scratch) for name in ('TEMP','TMP','TMPDIR')}
        module = api.root / 'observed_recovery_download.py'
        module.write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, encoding='utf-8')
        with api:
            asyncio.run(verify(api, result, baseline))
        result['final_api_returncode'] = api.proc.poll()
        assert result['final_api_returncode'] is not None
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__, 'message': str(error)[:1200]}
        raise
    finally:
        (api.root / 'recovery-download-admission.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('RECOVERY_DOWNLOAD_ADMISSION_OK ' + str(api.root), flush=True)


if __name__ == '__main__':
    run()
