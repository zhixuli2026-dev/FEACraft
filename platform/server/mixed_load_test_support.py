"""Disposable Linux stack and real ParaView connection for FF-J21 mixed load."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

import aiohttp

from testsupport import SERVER


def docker(*args, env=None, timeout=30):
    result = subprocess.run(['docker', *args], capture_output=True, encoding='utf-8',
                            errors='replace', env=env, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'Docker {args[0]} returned {result.returncode}')
    return result.stdout.strip()


API_IMAGE = 'vibeflow/api:j14-release-paraview-20261125'
WEB_IMAGE = 'vibeflow/web:j14-ingress-drain-offline-20261125'
opener = build_opener(ProxyHandler({}))


class Deployment:
    def __init__(self, stack, evidence, api_image, web_image):
        self.api_image = api_image
        self.web_image = web_image
        self.observer = Path(__file__).with_name('mixed_load_observer.py')
        self.stack = stack
        self.label = 'vf-j14-roll-' + uuid4().hex[:12]
        network = docker('network', 'create', self.label)
        self.stack.callback(docker, 'network', 'rm', network)
        self.network = network
        self.data = self.volume('data', '/work/data')
        self.code = self.volume('source', '/workspace/server')
        self.a, self.b, self.c = [f'{self.label}-{name}' for name in ('a', 'b', 'c')]
        self.upstreams = evidence / self.label / 'upstreams'
        self.upstreams.mkdir(parents=True)
        (self.upstreams / 'api.conf').write_bytes(
            ''.join(f'server {host}:8000 resolve;\n' for host in (self.a, self.b)).encode('ascii')
        )
        source = f'type=bind,source={SERVER},target=/source,readonly'
        options = ['--rm', '--network', 'none', '--pull=never']
        docker('run', *options, '--mount', self.code, '--mount', self.data,
               '--mount', source, '--entrypoint', '/bin/bash', self.api_image,
               '-o', 'pipefail', '-ec',
               'tar -C /source --exclude=__pycache__ --exclude="*.pyc" --exclude=.vault_key --exclude=".env*" -cf - '
               'app skills examples migrations alembic.ini data/completion | tar -C /workspace/server -xf -; '
               'chown 65534:65534 /work/data', timeout=135)
        pg = f'{self.label}-postgres'
        redis = f'{self.label}-redis'
        password = uuid4().hex + uuid4().hex
        self.run_container(pg, '--tmpfs', '/var/lib/postgresql/data:rw,size=512m', '-e', 'POSTGRES_PASSWORD',
                           '-e', 'POSTGRES_DB=replicas', 'postgres:16-alpine',
                           environment={'POSTGRES_PASSWORD': password})
        self.wait(lambda: docker('exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'replicas', timeout=4))
        self.run_container(redis, 'redis:7-alpine', 'redis-server', '--save', '', '--appendonly', 'no')
        self.wait(lambda: True if docker('exec', redis, 'redis-cli', 'ping', timeout=4) == 'PONG' else False)
        routes = json.dumps({host: f'http://{host}:8000' for host in (self.a, self.b, self.c)})
        self.env = {
            'VIBEFLOW_JWT_SECRET': uuid4().hex + uuid4().hex,
            'VIBEFLOW_DATABASE_URL': f'postgresql+asyncpg://postgres:{password}@{pg}:5432/replicas',
            'VIBEFLOW_REDIS_HOST': redis, 'VIBEFLOW_REDIS_PORT': '6379',
            'VIBEFLOW_DISTRIBUTED_JOBS': 'true',
            'VIBEFLOW_VIZ_OWNER_ROUTES': routes, 'VIBEFLOW_TERMINAL_OWNER_ROUTES': routes,
        }
        self.runtime = ['--mount', self.code + ',readonly', '--mount', self.data,
                        '--user', '65534:65534', '--workdir', '/workspace/server',
                        '--security-opt', 'seccomp=unconfined',
                        '-e', 'VIBEFLOW_STORAGE_BACKEND=fs', '-e', 'VIBEFLOW_STORAGE_FS_ROOT=/work/data/files',
                        '-e', 'VIBEFLOW_INDEX_ROOT=/work/data/index', '-e', 'VIBEFLOW_AUDIT_DIR=/work/data/audit',
                        '-e', 'VIBEFLOW_RUNTIME_CONFIG=/work/data/runtime.json',
                        '-e', 'VIBEFLOW_CODEX_STATE_ROOT=/work/data/cli-state',
                        '-e', 'VIBEFLOW_SKILLS_STORAGE_ROOT=/work/data/skills',
                        '-e', 'VIBEFLOW_STORAGE_MAINTENANCE_ENABLED=false',
                        '-e', 'VIBEFLOW_RATE_LIMIT_API_PER_MINUTE=10000',
                        '-e', 'VIBEFLOW_VIZ_IDLE_SECONDS=300', '-e', 'LP_NUM_THREADS=1',
                        '-e', 'OMP_NUM_THREADS=1', '-e', 'VTK_SMP_MAX_THREADS=1',
                        '-e', 'VIBEFLOW_VTK_OSMESA_DIR=/opt/paraview/lib/mesa']
        self.flags = [item for key in self.env for item in ('-e', key)]
        docker('run', '--rm', '--pull=never', '--network', network, *self.runtime, *self.flags,
               '--entrypoint', '/usr/local/bin/python', self.api_image, '-B', '-m', 'alembic', 'upgrade', 'head',
               env=dict(os.environ, **self.env), timeout=85)
        self.redis = redis
        self.worker = self.start_worker(self.label + '-worker')

    def start_worker(self, name):
        return self.run_container(name, *self.runtime, *self.flags,
                                  '--entrypoint', '/usr/local/bin/python', self.api_image,
                                  '-B', '-m', 'app.services.distributed_worker', environment=self.env)

    def volume(self, suffix, target):
        name = docker('volume', 'create', self.label + '-' + suffix)
        self.stack.callback(docker, 'volume', 'rm', name)
        return f'type=volume,source={name},target={target}'

    def run_container(self, name, *args, environment=None):
        container = docker('run', '--detach', '--pull=never', '--network', self.network, '--name', name, *args,
                           env=dict(os.environ, **(environment or {})), timeout=55)
        self.stack.callback(docker, 'rm', '--force', '--volumes', container)
        return container

    @staticmethod
    def wait(check, seconds=45):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if result := check():
                    return result
            except (OSError, RuntimeError, ValueError):
                pass
            time.sleep(.25)
        raise TimeoutError('A disposable deployment service was unavailable')

    def api(self, name):
        probe = f'type=bind,source={self.observer},target=/tests/mixed_load_observer.py,readonly'
        container = self.run_container(name, *self.runtime, *self.flags, '--network-alias', 'api',
                    '--hostname', name, '--mount', probe, '--publish', '127.0.0.1::8000',
                    '--entrypoint', '/usr/local/bin/python', self.api_image, '-B', '-m', 'uvicorn',
                    '--app-dir', '/tests', 'mixed_load_observer:app', '--host', '0.0.0.0', '--port', '8000', '--no-access-log',
                    environment=self.env)
        url = 'http://' + docker('port', container, '8000/tcp')
        assert url.startswith('http://127.0.0.1:')
        self.wait(lambda: json.load(opener.open(url + '/api/health', timeout=2))['ok'])
        return container, url

    def web(self, *, replicas=True):
        upstream_mount = f'type=bind,source={self.upstreams},target=/etc/nginx/vibeflow-upstreams,readonly'
        options = ['--mount', upstream_mount] if replicas else []
        suffix = '-web' if replicas else '-web-standalone'
        container = self.run_container(self.label + suffix, *options,
                                       '--publish', '127.0.0.1::80', self.web_image)
        url = 'http://' + docker('port', container, '80/tcp')
        assert url.startswith('http://127.0.0.1:')
        self.wait(lambda: json.load(opener.open(url + '/api/health', timeout=2))['ok'])
        return container, url


class RenderClient:
    def __init__(self, client, url, token, sid):
        self.client, self.url, self.token, self.sid = client, url, token, sid
        self.socket = None
        self.number = 0
        self.identity = 'system'

    async def connect(self):
        async with self.client.post(self.url + '/api/v1/connection-tickets',
                                    headers={'Authorization':'Bearer ' + self.token},
                                    json={'scope':'viz','session_id':str(self.sid)}) as response:
            assert response.status == 200, f'Connection ticket returned {response.status}'
            ticket = await response.json()
            assert ticket['url'] == f'/viz-proxy/{self.sid}/index.html'
            cookie, = response.cookies.values()
            assert cookie['path'] == f'/viz-proxy/{self.sid}/' and cookie['httponly']
            headers = {'Cookie': f'{cookie.key}={cookie.value}'}
        try:
            self.socket = await self.client.ws_connect(f'{self.url}/viz-proxy/{self.sid}/ws',
                headers=headers,max_msg_size=4*1024*1024)
        except aiohttp.WSServerHandshakeError as error:
            raise RuntimeError(f'Visualization WebSocket authorization returned {error.status}') from None
        self.identity = 'system'
        reply = await self.rpc('wslink.hello',[{'secret':'wslink-secret'}])
        self.identity = reply['clientID']
        await self.rpc('viewport.size.update',['-1',480,360])
        await self.rpc('viewport.image.push.observer.add',['-1'])

    async def send(self, method, args):
        self.number += 1
        identifier = f'{self.identity}:{self.number}'
        await self.socket.send_json({'wslink':'1.0','id':identifier,'method':method,'args':args})
        return identifier

    async def rpc(self, method, args):
        identifier = await self.send(method,args)
        async with asyncio.timeout(30):
            while True:
                message = await self.socket.receive()
                if message.type==aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if data.get('id')==identifier:
                        if 'error' in data:
                            raise RuntimeError(f'{method} returned {data["error"]}')
                        return data.get('result')
                elif message.type not in (aiohttp.WSMsgType.BINARY,aiohttp.WSMsgType.PING,aiohttp.WSMsgType.PONG):
                    raise OSError(f'Visualization WebSocket closed: {message.type}')

    async def render(self):
        return await self.call_for_image('viewport.image.push',[{'view':'-1'}])

    async def drag(self, index, quality=100):
        if quality != 100:
            await self.rpc('viewport.image.push.quality', ['-1', quality, 1])
        event = {'view':'-1','action':'down','x':.62,'y':.50,'buttonLeft':True,'buttonMiddle':False,
                 'buttonRight':False,'shiftKey':False,'ctrlKey':False,'altKey':False,'metaKey':False}
        await self.rpc('viewport.mouse.interaction',[event])
        # A separate list round trip drains the camera-down notifications.
        await self.rpc('pv.proxy.manager.list',[])
        changed = {**event,'action':'move','x':.68+.015*index,'y':.56}
        latency, frame = await self.call_for_image('viewport.mouse.interaction',[changed])
        await self.rpc('viewport.mouse.interaction',[{**changed,'action':'up','buttonLeft':False}])
        if quality != 100:
            await self.rpc('viewport.image.push.quality', ['-1', 100, 1])
        return latency,frame

    async def call_for_image(self,method,args):
        started = time.perf_counter()
        pixels = None
        measured = {'ack_ms': None, 'jpeg_ms': None, 'publication_ms': None, 'jpeg_count': 0,
                    'publication_count': 0, 'method': method}
        identifier = await self.send(method,args)
        async with asyncio.timeout(6):
            while (measured['jpeg_ms'] is None or measured['ack_ms'] is None
                   or measured['publication_ms'] is None):
                message = await self.socket.receive()
                elapsed = round((time.perf_counter() - started)*1000, 3)
                if message.type==aiohttp.WSMsgType.BINARY and message.data.startswith(b'\xff\xd8\xff'):
                    measured['jpeg_count'] += 1
                    if pixels is None:
                        measured['jpeg_ms'] = elapsed
                    pixels = message.data
                elif message.type==aiohttp.WSMsgType.TEXT:
                    row = json.loads(message.data)
                    if row.get('id')==identifier:
                        if 'error' in row:
                            raise RuntimeError(str(row['error']))
                        measured['ack_ms'] = elapsed
                    if (row.get('id') or '').startswith('publish:viewport.image.push.subscription:'):
                        measured['publication_count'] += 1
                        info = row['result']
                        if isinstance(info, list):
                            info = info[0]
                        measured.update(publication_ms=elapsed, render_ms=info.get('workTime'),
                                        dimensions=info.get('size'), stale=info.get('stale'))
                elif message.type not in (aiohttp.WSMsgType.BINARY,aiohttp.WSMsgType.PING,aiohttp.WSMsgType.PONG):
                    raise OSError(f'Visualization socket closed: message={message.type.name}, code={self.socket.close_code}')
        return measured['publication_ms'],pixels
