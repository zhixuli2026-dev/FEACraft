"""Exercise real ParaView sessions in distinct Linux cgroups. No inference."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import time

import aiohttp

from mixed_load_test_support import API_IMAGE, WEB_IMAGE, Deployment, RenderClient, docker


async def request(client, method, url, *, token=None, expected=200, **options):
    headers = {'Authorization': 'Bearer '+token} if token else {}
    async with client.request(method, url, headers=headers, **options) as response:
        payload = await response.json()
        assert response.status == expected, (method, url, response.status, payload)
        return payload


async def case_with_grid(client, url, contents, *, engine='paraview'):
    api = url+'/api/v1'
    handle = 'viz-compute-'+secrets.token_hex(8)
    auth = await request(client, 'POST', api+'/auth/register', json={
        'email': handle+'@example.com', 'name': handle, 'password': secrets.token_hex(20)})
    token = auth['token']
    workspace = (await request(client, 'GET', api+'/workspaces', token=token))[0]['id']
    case = await request(client, 'POST', f'{api}/workspaces/{workspace}/cases', token=token,
                         json={'name': 'Isolated renderer compute'})
    base = f'{api}/cases/{case["id"]}'
    path = 'D-post/compute-proof.vtp'
    upload = await request(client, 'POST', base+'/uploads', token=token, json={
        'path': path, 'size': len(contents), 'sha256': hashlib.sha256(contents).hexdigest(), 'overwrite': False})
    endpoint = base+'/uploads/'+upload['id']
    for offset in range(0, len(contents), 8*1024**2):
        await request(client, 'PUT', endpoint, token=token, params={'offset': offset}, data=contents[offset:offset+8*1024**2])
    await request(client, 'POST', endpoint+':complete', token=token)
    return token, base+'/viz:sessions', {'file_path': path, 'engine': engine}


def verify_stop(row, *, cpu, memory, reason):
    limit = None if reason == 'cancelled' else reason
    stop = row['stop']
    assert stop['verified'] and stop['scope_empty'] and stop['reason'] == reason, row
    assert row['phase'] == ('failed' if limit else 'closed')
    assert row['error'] == ('VizResourceLimit' if limit else None)
    meter = stop['linux_compute_usage']
    assert meter['scope']['policy'] == {'cpu_limit_ns': cpu*1_000_000_000, 'memory_limit_bytes': memory*1024**2}
    assert meter['launch_nonce'] == row['process']['launch_nonce'] and meter['violation'] == limit
    assert row['process']['namespace_init']['pid_namespace'] != row['process']['host']['pid_namespace']
    return meter


async def verify(endpoints, contents, receipt):
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=40)) as client:
        normal, constrained_cpu, constrained_memory, legacy, unavailable = endpoints
        left, right = await asyncio.gather(case_with_grid(client, constrained_cpu, contents),
                                           case_with_grid(client, constrained_cpu, contents))
        first, second = await asyncio.gather(*(request(client, 'POST', route, token=token, json=body)
                                             for token, route, body in (left, right)))
        assert first['phase'] == second['phase'] == 'active'
        keep = RenderClient(client, constrained_cpu, left[0], first['id'])
        drive = RenderClient(client, constrained_cpu, right[0], second['id'])
        try:
            await asyncio.gather(keep.connect(), drive.connect())
            for _, frame in await asyncio.gather(keep.render(), drive.render()):
                assert frame.startswith(b'\xff\xd8\xff') and frame.endswith(b'\xff\xd9')
            endpoint = f'{constrained_cpu}/api/v1/viz:sessions/{second["id"]}'
            await request(client, 'GET', endpoint, token=left[0], expected=404)
            renders = 1
            until = time.monotonic() + 65
            while time.monotonic() < until:
                try:
                    await drive.render()
                except (OSError, TimeoutError, aiohttp.ClientError):
                    break
                renders += 1
            while time.monotonic() < until:
                cpu_row = await request(client, 'GET', endpoint, token=right[0])
                if cpu_row['phase'] == 'failed':
                    break
                await asyncio.sleep(.15)
            else:
                raise TimeoutError('The busy renderer did not stop at its CPU budget')
            usage = verify_stop(cpu_row, cpu=6, memory=768, reason='cpu_limit')
            assert usage['usage']['cpu_used_ns'] >= 6_000_000_000
            receipt['cpu'] = {'completed_jpegs': renders, 'record': cpu_row}
            print('VIZ_COMPUTE_CPU', renders, usage['usage']['cpu_used_ns'], flush=True)

            status_url = f'{constrained_cpu}/api/v1/viz:sessions/{first["id"]}'
            assert (await request(client, 'GET', status_url, token=left[0]))['phase'] == 'active'
            _, image = await keep.render()
            assert image.endswith(b'\xff\xd9')
            await request(client, 'DELETE', status_url, token=left[0])
            ordinary = await request(client, 'GET', status_url, token=left[0])
            sampled = verify_stop(ordinary, cpu=6, memory=768, reason='cancelled')
            assert sampled['scope']['nonce'] != usage['scope']['nonce']
            assert sampled['usage']['cpu_used_ns'] > 0 and sampled['usage']['memory_peak_bytes'] > 0
            peer_row = await request(client, 'GET', f'{normal}/api/v1/viz:sessions/{first["id"]}', token=left[0])
            assert peer_row['stop'] == ordinary['stop']
            receipt['sibling'] = ordinary
            print('VIZ_COMPUTE_SIBLING', sampled['usage'], flush=True)
        finally:
            for view in (keep, drive):
                if view.socket is not None:
                    await view.socket.close()

        token, route, payload = await case_with_grid(client, constrained_memory, contents)
        fail = await request(client, 'POST', route, token=token, json=payload, expected=409)
        assert fail['detail']['code'] == 'viz_memory_limit', fail
        listing = await request(client, 'GET', route, token=token, params={'include_closed': 'true'})
        assert len(listing) == 1
        row = listing[0]
        usage = verify_stop(row, cpu=20, memory=48, reason='memory_limit')
        assert row['ready'] is None and usage['usage']['memory_oom_kills'] >= 1
        receipt['memory'] = row
        print('VIZ_COMPUTE_MEMORY', usage['usage'], flush=True)

        token, route, payload = await case_with_grid(client, normal, contents, engine='trame')
        trame = await request(client, 'POST', route, token=token, json=payload)
        assert trame['engine'] == 'trame' and trame['phase'] == 'active'
        async with client.post(normal+'/api/v1/connection-tickets', headers={'Authorization': 'Bearer '+token},
                               json={'scope': 'viz', 'session_id': str(trame['id'])}) as ticket:
            assert ticket.status == 200
            page = await ticket.json()
            cookie, = ticket.cookies.values()
        async with client.get(normal+page['url'], headers={'Cookie': f'{cookie.key}={cookie.value}'}) as index:
            assert index.status == 200 and 'text/html' in index.headers['Content-Type'] and await index.read()
        endpoint = f'{normal}/api/v1/viz:sessions/{trame["id"]}'
        await request(client, 'DELETE', endpoint, token=token)
        trame = await request(client, 'GET', endpoint, token=token)
        usage = verify_stop(trame, cpu=20, memory=768, reason='cancelled')
        assert usage['usage']['cpu_used_ns'] > 0 and usage['usage']['memory_peak_bytes'] > 0
        receipt['trame'] = trame
        print('VIZ_COMPUTE_TRAME', usage['usage'], flush=True)

        token, route, payload = await case_with_grid(client, legacy, contents)
        compatible = await request(client, 'POST', route, token=token, json=payload)
        endpoint = f'{legacy}/api/v1/viz:sessions/{compatible["id"]}'
        await request(client, 'DELETE', endpoint, token=token)
        compatible = await request(client, 'GET', endpoint, token=token)
        assert compatible['phase'] == 'closed' and compatible['stop']['verified']
        assert 'linux_compute_usage' not in compatible['stop']
        receipt['default_limits'] = compatible

        token, route, payload = await case_with_grid(client, unavailable, contents)
        fault = await request(client, 'POST', route, token=token, json=payload, expected=409)
        assert fault['detail']['code'] == 'viz_compute_unverified', fault
        history = await request(client, 'GET', route, token=token, params={'include_closed': 'true'})
        assert history == []
        receipt['unavailable'] = {'code': fault['detail']['code'], 'reserved_sessions': len(history)}
        print('VIZ_COMPUTE_DEFAULT_AND_UNAVAILABLE', flush=True)


def main(args):
    root = Path(tempfile.mkdtemp(prefix='j16-viz-compute-', dir=Path(__file__).resolve().parents[2]/'tmp'))
    original = set(docker('ps', '-q', '--no-trunc').splitlines())
    receipt = {'passed': False, 'actual_model_calls': 0, 'simulated_model_calls': 0}
    print('VIZ_COMPUTE_EVIDENCE', root, flush=True)
    try:
        with ExitStack() as stack:
            deploy = Deployment(stack, root, args.api_image, WEB_IMAGE)
            deploy.runtime = ['0:0' if option == '65534:65534' else option for option in deploy.runtime]
            base = list(deploy.runtime)
            compute = ['--cgroupns', 'private', '--cap-add', 'SYS_ADMIN']
            endpoints, containers = [], []
            variants = ((deploy.a, 20, 768, compute), (deploy.b, 6, 768, compute), (deploy.c, 20, 48, compute),
                        (deploy.label+'-default', 0, 0, []), (deploy.label+'-unavailable', 6, 768, ['--cgroupns', 'private']))
            for name, cpu, memory, runtime in variants:
                deploy.runtime[:] = [*base, *runtime, '-e', f'VIBEFLOW_VIZ_SESSION_LINUX_CPU_LIMIT_SECONDS={cpu}',
                                     '-e', f'VIBEFLOW_VIZ_SESSION_LINUX_MEMORY_LIMIT_MB={memory}']
                container, endpoint = deploy.api(name)
                containers.append(container)
                endpoints.append(endpoint)
            asyncio.run(verify(endpoints, args.grid.read_bytes(), receipt))
            script = '''
from pathlib import Path
groups = sum(p.name.startswith('vibeflow-compute-') for p in Path('/sys/fs/cgroup').rglob('*'))
renderers = 0
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = path.read_bytes().split(b'\\0')
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    renderers += any(arg.rsplit(b'/', 1)[-1] in (b'viz_kernel.py', b'viz_paraview_kernel.py') for arg in args)
print(groups, renderers)
'''
            for container in containers:
                assert docker('exec', container, 'python', '-c', script) == '0 0'
            receipt['passed'] = True
    except Exception as error:
        receipt['failure'] = {'type': type(error).__name__, 'detail': str(error)[:3500]}
        raise
    finally:
        receipt['original_services'] = original == set(docker('ps', '-q', '--no-trunc').splitlines())
        receipt['passed'] = receipt['passed'] and receipt['original_services']
        (root/'result.json').write_text(json.dumps(receipt, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    assert receipt['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--grid', required=True, type=Path)
    parser.add_argument('--api-image', default=API_IMAGE)
    main(parser.parse_args())
