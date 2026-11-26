"""Exercise actual Linux PTY commands and their kernel quotas. No model calls."""

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import json
from pathlib import Path
import re
import secrets
import shlex
import tempfile

import aiohttp

from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, docker


async def request(client, endpoint, method, path, *, token=None, expected=200, **options):
    headers = {'Authorization': 'Bearer '+token} if token else {}
    async with client.request(method, endpoint+'/api/v1'+path, headers=headers, **options) as response:
        payload = await response.json()
        assert response.status == expected, (method, path, response.status, payload)
        return payload


async def account(client, endpoint):
    tag = secrets.token_hex(8)
    auth = await request(client, endpoint, 'POST', '/auth/register', json={
        'email': tag+'@example.com', 'name': 'PTY quota '+tag, 'password': secrets.token_hex(20)})
    token = auth['token']
    workspace = (await request(client, endpoint, 'GET', '/workspaces', token=token))[0]['id']
    case = await request(client, endpoint, 'POST', f'/workspaces/{workspace}/cases', token=token,
                         json={'name': 'Isolated PTY compute'})
    return token, case['id']


async def open_pty(stack, client, endpoint, token, case):
    socket = await stack.enter_async_context(client.ws_connect(endpoint+'/ws/terminal',
        params={'case': case, 'shell': 'cmd'}, headers={'Authorization': 'Bearer '+token}))
    message = await socket.receive_json(timeout=30)
    return socket, message


async def output(socket, pattern):
    buffer = b''
    async with asyncio.timeout(20):
        while True:
            message = await socket.receive()
            if message.type == aiohttp.WSMsgType.TEXT and json.loads(message.data).get('type') == 'resize_accepted':
                continue
            if message.type != aiohttp.WSMsgType.BINARY:
                raise RuntimeError(f'PTY output ended before the expected evidence: {message.data}')
            buffer = (buffer+message.data)[-32768:]
            match = re.search(pattern, buffer)
            if match:
                return match


async def assert_limit(socket, reason):
    async with asyncio.timeout(25):
        while True:
            message = await socket.receive()
            if message.type == aiohttp.WSMsgType.TEXT:
                payload = json.loads(message.data)
                assert payload['type'] == 'error' and payload['code'] == 'terminal_resource_limit'
                assert payload['reason'] == reason, payload
                return
            assert message.type == aiohttp.WSMsgType.BINARY, message.type


async def command(socket, body):
    await socket.send_json({'type': 'input', 'data': body+'\r'})


def verified(row, seconds, memory, *, reason):
    stop = row['stop']
    assert stop['verified'] and stop['scope_empty'] and stop['reason'] == reason, row
    quota = reason != 'cancelled'
    assert row['phase'] == ('failed' if quota else 'closed')
    assert row['error'] == ('TerminalResourceLimit' if quota else None)
    root = row['process']['root']
    assert root['namespace_init']['pid_namespace'] != root['host']['pid_namespace']
    report = stop['linux_compute_usage']
    assert report['launch_nonce'] == root['launch_nonce']
    assert report['scope']['policy'] == {'cpu_limit_ns': seconds*1_000_000_000, 'memory_limit_bytes': memory*1024**2}
    assert report['violation'] == (reason if quota else None)
    return report


CPU_CHILDREN = '''
import json
from pathlib import Path
import subprocess
import sys
code = "import time\\nend=time.process_time()+0.72\\nwhile time.process_time()<end: pass"
children = [subprocess.Popen([sys.executable, '-c', code]) for _ in range(2)]
members = {p.pid: (Path('/proc')/str(p.pid)/'cgroup').read_text() for p in children}
print('VF_'+'PTY_CHILDREN='+json.dumps(members), flush=True)
for process in children:
    process.wait()
'''


async def verify(endpoints, receipt):
    cpu, memory, legacy, unavailable = endpoints
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(aiohttp.ClientSession(trust_env=False,
                                                timeout=aiohttp.ClientTimeout(total=40)))
        left, right = await asyncio.gather(account(client, cpu), account(client, cpu))
        busy, first = await open_pty(stack, client, cpu, *left)
        sibling, second = await open_pty(stack, client, cpu, *right)
        assert first['type'] == second['type'] == 'ready'
        first_path, second_path = ('/terminal/sessions/'+ready['session'] for ready in (first, second))
        await request(client, cpu, 'GET', first_path, token=right[0], expected=404)
        await command(busy, 'python3 -c '+shlex.quote(CPU_CHILDREN))
        match = await output(busy, rb'VF_PTY_CHILDREN=(\{[^\r\n]+\})')
        members = json.loads(match.group(1))
        assert len(members) == 2 and all(int(pid) > 1 for pid in members)
        await assert_limit(busy, 'cpu_limit')
        row = await request(client, cpu, 'GET', first_path, token=left[0])
        report = verified(row, 1, 192, reason='cpu_limit')
        assert report['usage']['cpu_used_ns'] >= 1_000_000_000
        group = 'vibeflow-compute-'+report['launch_nonce']
        assert all(any('cpuacct' in line and line.endswith('/'+group) for line in groups.splitlines())
                   for groups in members.values())
        receipt['cpu'] = {'record': row, 'children': len(members)}
        print('PTY_COMPUTE_CPU', report['usage']['cpu_used_ns'], flush=True)

        await sibling.send_json({'type': 'resize', 'cols': 93, 'rows': 29})
        await command(sibling, "printf '\\n%s%s\\n' 'PTY_' 'SIBLING_OK'; stty size")
        await output(sibling, rb'PTY_SIBLING_OK\r?\n29 93')
        await request(client, cpu, 'DELETE', second_path, token=right[0])
        sibling_row = await request(client, cpu, 'GET', second_path, token=right[0])
        other = verified(sibling_row, 1, 192, reason='cancelled')
        assert other['launch_nonce'] != report['launch_nonce'] and other['usage']['cpu_used_ns'] > 0
        peer = await request(client, legacy, 'GET', second_path, token=right[0])
        assert peer['stop'] == sibling_row['stop']
        receipt['sibling'] = sibling_row

        token, case = await account(client, memory)
        socket, active = await open_pty(stack, client, memory, token, case)
        assert active['type'] == 'ready', active
        await command(socket, "python3 -c 'a=bytearray(160*1024**2); print(len(a))'")
        await assert_limit(socket, 'memory_limit')
        row = await request(client, memory, 'GET', '/terminal/sessions/'+active['session'], token=token)
        report = verified(row, 20, 64, reason='memory_limit')
        assert report['usage']['memory_oom_kills'] >= 1
        receipt['memory'] = row
        print('PTY_COMPUTE_MEMORY', report['usage'], flush=True)

        token, case = await account(client, legacy)
        socket, active = await open_pty(stack, client, legacy, token, case)
        assert active['type'] == 'ready'
        await command(socket, "printf '\\n%s%s\\n' 'PTY_' 'DEFAULT_OK'")
        await output(socket, rb'PTY_DEFAULT_OK')
        path = '/terminal/sessions/'+active['session']
        await request(client, legacy, 'DELETE', path, token=token)
        row = await request(client, legacy, 'GET', path, token=token)
        assert row['stop']['verified'] and 'linux_compute_usage' not in row['stop']
        receipt['default'] = row

        token, case = await account(client, unavailable)
        _, error = await open_pty(stack, client, unavailable, token, case)
        assert error['type'] == 'error' and error['reason'] == 'compute_unverified', error
        listing = await request(client, unavailable, 'GET', '/terminal/sessions', token=token,
                                params={'include_closed': 'true'})
        assert listing['sessions'] == [], listing
        receipt['unavailable'] = {'code': error['reason'], 'reserved_sessions': len(listing['sessions'])}


def main(args):
    root = Path(tempfile.mkdtemp(prefix='j16-pty-compute-', dir=Path(__file__).resolve().parents[2]/'tmp'))
    original = set(docker('ps', '-q', '--no-trunc').splitlines())
    receipt = {'passed': False, 'actual_model_calls': 0, 'simulated_model_calls': 0}
    print('PTY_COMPUTE_EVIDENCE', root, flush=True)
    try:
        with ExitStack() as stack:
            deploy = Deployment(stack, root, args.api_image, WEB_IMAGE)
            base = ['0:0' if option == '65534:65534' else option for option in deploy.runtime]
            namespace = ['--cgroupns', 'private']
            compute = [*namespace, '--cap-add', 'SYS_ADMIN']
            variants = ((deploy.a, 1, 192, compute), (deploy.b, 20, 64, compute),
                        (deploy.c, 0, 0, []), (deploy.label+'-unavailable', 1, 192, namespace))
            containers, endpoints = [], []
            for name, seconds, memory, flags in variants:
                deploy.runtime[:] = [*base, *flags, '-e', f'VIBEFLOW_TERMINAL_SESSION_LINUX_CPU_LIMIT_SECONDS={seconds}',
                                     '-e', f'VIBEFLOW_TERMINAL_SESSION_LINUX_MEMORY_LIMIT_MB={memory}']
                container, url = deploy.api(name)
                containers.append(container)
                endpoints.append(url)
            asyncio.run(verify(endpoints, receipt))
            probe = '''
from pathlib import Path
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = path.read_bytes().split(b'\\0')
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    assert not any(arg.rsplit(b'/', 1)[-1] in (b'terminal_bridge.py', b'terminal_pty_gate.py') for arg in args)
assert not any(p.name.startswith('vibeflow-compute-') for p in Path('/sys/fs/cgroup').rglob('*'))
print('EMPTY')
'''
            for container in containers:
                assert docker('exec', container, 'python', '-c', probe) == 'EMPTY'
            receipt['passed'] = True
    finally:
        receipt['original_services'] = original == set(docker('ps', '-q', '--no-trunc').splitlines())
        receipt['passed'] = receipt['passed'] and receipt['original_services']
        (root/'result.json').write_text(json.dumps(receipt, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    assert receipt['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-image', default=API_IMAGE)
    main(parser.parse_args())
