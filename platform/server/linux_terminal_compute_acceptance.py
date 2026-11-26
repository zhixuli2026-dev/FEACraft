"""Exercise actual Linux PTY isolation, interactions and kernel quotas. No model calls."""

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import json
from pathlib import Path
import re
import secrets
import shlex
import subprocess
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


async def open_pty(stack, client, endpoint, token, case=0, session=''):
    socket = await stack.enter_async_context(client.ws_connect(endpoint+'/ws/terminal',
        params={'case': case, 'shell': 'cmd', 'session': session}, headers={'Authorization': 'Bearer '+token}))
    message = await socket.receive_json(timeout=30)
    return socket, message


async def output(socket, pattern):
    buffer = b''
    try:
        async with asyncio.timeout(20):
            while True:
                message = await socket.receive()
                if message.type == aiohttp.WSMsgType.TEXT and json.loads(message.data).get('type') == 'resize_accepted':
                    continue
                if message.type != aiohttp.WSMsgType.BINARY:
                    raise RuntimeError(f'PTY output ended before {pattern!r}: {message.data}')
                buffer = (buffer+message.data)[-32768:]
                match = re.search(pattern, buffer)
                if match:
                    return match
    except TimeoutError as error:
        raise AssertionError(f'PTY output did not contain {pattern!r}: {buffer[-2048:]!r}') from error


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


async def put_marker(client, endpoint, owner):
    token, case = owner
    form = aiohttp.FormData()
    form.add_field('file', b'authorized case marker', filename='pty-marker.txt')
    await request(client, endpoint, 'POST', f'/cases/{case}/upload', token=token,
                  params={'path': 'pty-marker.txt'}, data=form)


async def expect_file(client, endpoint, owner, name, content):
    token, case = owner
    async with client.get(endpoint+f'/api/v1/cases/{case}/raw',
                          headers={'Authorization': 'Bearer '+token}, params={'path': name}) as response:
        assert response.status == 200 and await response.read() == content


async def close_pty(client, endpoint, token, active):
    path = '/terminal/sessions/'+active['session']
    await request(client, endpoint, 'DELETE', path, token=token)
    row = await request(client, endpoint, 'GET', path, token=token)
    assert row['phase'] == 'closed' and row['stop']['verified'] and row['stop']['scope_empty'], row
    assert row['process']['gate']['linux_launcher_sha256']
    return row


async def verify_workspace(owner_api, peer_api, receipt, network_host):
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(aiohttp.ClientSession(trust_env=False,
                                               timeout=aiohttp.ClientTimeout(total=45)))
        owner, other = await asyncio.gather(account(client, owner_api), account(client, owner_api))
        await put_marker(client, owner_api, owner)
        await put_marker(client, owner_api, other)
        sibling, sibling_ready = await open_pty(stack, client, owner_api, *other)
        assert sibling_ready['type'] == 'ready', sibling_ready
        await command(sibling, """if test -r pty-marker.txt; then printf '\n%s%s=%s\n' 'VF_' 'PTY_DIR' "$PWD"; fi""")
        match = await output(sibling, rb'VF_PTY_DIR=(/[^\r\n]+)')
        sibling_dir = match.group(1).decode()

        socket, active = await open_pty(stack, client, owner_api, *owner)
        assert active['type'] == 'ready', active
        path = '/terminal/sessions/'+active['session']
        await request(client, owner_api, 'GET', path, token=other[0], expected=404)
        program = f"""from pathlib import Path
import os
import socket
assert Path('pty-marker.txt').read_bytes() == b'authorized case marker'
assert not Path({sibling_dir!r}, 'pty-marker.txt').exists()
assert not Path('/workspace/server/app/main.py').exists()
assert not Path('/tmp/pty-host-marker').exists()
assert 'VIBEFLOW_JWT_SECRET' not in os.environ
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'], 16) == int(status['CapBnd'], 16) == 0
assert status['NoNewPrivs'].strip() == '1'
mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
assert any(entry[4] == '/usr' and 'ro' in entry[5].split(',') for entry in mounts)
try:
    with socket.create_connection(({network_host!r}, 6379), timeout=0.5):
        pass
except OSError:
    pass
else:
    raise AssertionError('Guest connected to the disposable listener')
Path('pty-output.txt').write_bytes(b'from isolated PTY')
Path('/tmp/pty-private-marker').touch()
print('VF_'+'PTY_SCOPED', flush=True)"""
        await command(socket, 'python3 -c '+shlex.quote(program))
        await output(socket, rb'VF_PTY_SCOPED')
        await close_pty(client, owner_api, other[0], sibling_ready)
        await command(socket, """python3 -c 'import time; print("VF_"+"PTY_FOREGROUND",flush=True); time.sleep(30)'""")
        await output(socket, rb'VF_PTY_FOREGROUND')
        await socket.send_json({'type': 'input', 'data': '\x03'})
        await command(socket, """test "$?" -eq 130 && printf '\n%s%s\n' 'VF_' 'PTY_INTERRUPT_OK'""")
        await output(socket, rb'VF_PTY_INTERRUPT_OK')
        await socket.close()
        socket, reattached = await open_pty(stack, client, peer_api, *owner, session=active['session'])
        assert reattached == {'type': 'ready', 'session': active['session'], 'alive': True}, reattached
        await socket.send_json({'type': 'resize', 'cols': 107, 'rows': 34})
        await command(socket, """test -f /tmp/pty-private-marker && printf '\n%s%s\n' 'VF_' 'PTY_REATTACHED' && stty size""")
        await output(socket, rb'VF_PTY_REATTACHED\r?\n34 107')
        receipt['case'] = await close_pty(client, owner_api, owner[0], active)
        peer = await request(client, peer_api, 'GET', path, token=owner[0])
        assert peer['stop'] == receipt['case']['stop']
        await expect_file(client, peer_api, owner, 'pty-output.txt', b'from isolated PTY')
        await expect_file(client, peer_api, other, 'pty-marker.txt', b'authorized case marker')

        socket, active = await open_pty(stack, client, owner_api, owner[0])
        assert active['type'] == 'ready', active
        await command(socket, f"""test "$PWD" = /tmp && test ! -e {shlex.quote(sibling_dir)} &&
test ! -e /tmp/pty-host-marker && test ! -e /tmp/pty-private-marker &&
printf '\n%s%s\n' 'VF_' 'PTY_SCRATCH_ONLY'""")
        await output(socket, rb'VF_PTY_SCRATCH_ONLY')
        await command(socket, 'exit 7')
        async with asyncio.timeout(20):
            while True:
                message = await socket.receive()
                if message.type == aiohttp.WSMsgType.TEXT:
                    assert json.loads(message.data) == {'type': 'exit', 'code': 7}, message.data
                    break
                assert message.type == aiohttp.WSMsgType.BINARY, message.type
        row = await request(client, owner_api, 'GET', '/terminal/sessions/'+active['session'], token=owner[0])
        assert row['phase'] == 'closed' and row['exit_code'] == 7
        assert row['stop']['verified'] and row['stop']['scope_empty'], row
        receipt['scratch'] = row
        receipt.update(cross_account_status=404, peer_reconnect=True, peer_file_readback=True,
                       private_scratch=True, filtered_environment=True, runtime_readonly=True,
                       capabilities_empty=True, ctrl_c_exit_status=130, resize=[34, 107], network_isolated=True)


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
    receipt = {'passed': False, 'storage': args.storage, 'actual_model_calls': 0, 'simulated_model_calls': 0}
    print('PTY_COMPUTE_EVIDENCE', root, flush=True)
    try:
        with ExitStack() as stack:
            deploy = Deployment(stack, root, args.api_image, WEB_IMAGE)
            base = ['0:0' if option == '65534:65534' else option for option in deploy.runtime]
            namespace = ['--cgroupns', 'private']
            compute = [*namespace, '--cap-add', 'SYS_ADMIN']
            unavailable = deploy.label+'-unavailable'
            variants = ((deploy.a, 1, 192, base, compute), (deploy.b, 20, 64, base, compute),
                        (deploy.c, 0, 0, base, []), (unavailable, 1, 192, base, namespace))
            if args.storage == 's3':
                deploy.configure_s3(root)
                docker('run', '--rm', '--network', 'none', '--pull=never', '--mount', deploy.data,
                       '--entrypoint', '/usr/bin/install', args.api_image, '-d', '-o', '65534', '-g', '65534',
                       '/work/data/files')
                runtime = deploy.runtime.copy()
                cold_cache = ['--tmpfs', '/work/data/files:rw,size=256m,uid=65534,gid=65534']
                variants = ((deploy.c, 0, 0, runtime, []), (deploy.a, 0, 0, runtime, cold_cache))
            containers, endpoints = {}, {}
            for name, seconds, memory, runtime, flags in variants:
                deploy.runtime[:] = [*runtime, *flags, '-e', f'VIBEFLOW_TERMINAL_SESSION_LINUX_CPU_LIMIT_SECONDS={seconds}',
                                     '-e', f'VIBEFLOW_TERMINAL_SESSION_LINUX_MEMORY_LIMIT_MB={memory}']
                container, url = deploy.api(name)
                containers[name] = container
                endpoints[name] = url
            if args.storage == 'fs':
                asyncio.run(verify([endpoints[name] for name in (deploy.a, deploy.b, deploy.c, unavailable)], receipt))
            else:
                assert docker('exec', containers[deploy.a], 'findmnt', '-n', '-o', 'FSTYPE',
                              '/work/data/files') == 'tmpfs'
                assert docker('exec', containers[deploy.a], 'find', '/work/data/files', '-type', 'f') == ''
                receipt['cold_peer_cache'] = True
            redis_ip = docker('inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',
                              deploy.label+'-redis')
            canary = """from pathlib import Path
import os
import socket
import sys
Path('/tmp/pty-host-marker').touch()
with socket.create_connection((sys.argv[1], 6379), timeout=2):
    print(os.getuid())
"""
            uid = 65534 if args.storage == 's3' else 0
            assert int(docker('exec', containers[deploy.c], 'python', '-B', '-c', canary, redis_ip)) == uid
            receipt['isolation'] = {}
            try:
                asyncio.run(verify_workspace(endpoints[deploy.c], endpoints[deploy.a], receipt['isolation'], redis_ip))
            except Exception:
                for role in (deploy.c, deploy.a):
                    api_log = subprocess.run(['docker', 'logs', '--tail', '180', containers[role]],
                        capture_output=True, encoding='utf-8', errors='replace', timeout=10)
                    log = api_log.stdout + '\n' + api_log.stderr
                    for name, value in deploy.env.items():
                        if any(credential in name for credential in ('SECRET', 'KEY', 'DATABASE_URL')):
                            log = log.replace(value, '[REDACTED]')
                    (root/f'{role}.log').write_text(log, encoding='utf-8')
                raise
            receipt['service_uid'] = uid
            print('PTY_ISOLATION_WORKSPACE', args.storage, flush=True)
            probe = '''
from pathlib import Path
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = path.read_bytes().split(b'\\0')
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    assert not any(arg.rsplit(b'/', 1)[-1] in (b'terminal_bridge.py', b'terminal_pty_gate.py') for arg in args)
assert not any(p.name.startswith('vibeflow-compute-') for p in Path('/sys/fs/cgroup').rglob('*'))
assert not list(Path('/work/data/files/.staging').glob('job-*'))
assert not Path('/tmp/pty-private-marker').exists()
print('EMPTY')
'''
            for container in containers.values():
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
    parser.add_argument('--storage', choices=('fs', 's3'), default='fs')
    main(parser.parse_args())
