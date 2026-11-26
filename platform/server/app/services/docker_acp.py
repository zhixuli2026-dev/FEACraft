"""Trusted host transport for the installed ACP agents inside Docker."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from uuid import UUID, uuid5

from ..config import get_settings
from . import acp_case_files, docker_cli_usage
from .async_ops import finish_before_cancel
from .docker_cli_profile import container_flags
from .docker_cli_state import StateVolume
from .owned_stdio import MAX_LINE


SCHEMA = 'vibeflow.acp.docker.v1'
LABEL = 'vibeflow.acp.execution'
ROOT = Path(__file__).resolve().parent


def command(*args):
    try:
        timeout = 25 if args[0] == 'info' else 18
        result = subprocess.run([get_settings().docker_cli_client, *args], capture_output=True, text=True,
                                encoding='utf8', timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'Docker command {args[0]} timed out') from error
    if result.returncode:
        raise RuntimeError(f'Docker command {args[0]} failed ({result.returncode})')
    return result.stdout.strip()


def engine_identity():
    if os.name == 'nt':
        endpoint = os.environ.get('DOCKER_HOST') if not os.environ.get('DOCKER_CONTEXT') else None
        if endpoint is None:
            endpoint = command('context', 'inspect', '--format', '{{.Endpoints.docker.Host}}')
        pipe = re.fullmatch(r'npipe:////\./pipe/([a-zA-Z0-9_-]{1,80})', endpoint)
        if pipe:
            try:
                result = subprocess.run([sys.executable, '-I', '-B', str(ROOT/'docker_engine_identity.py'),
                                         pipe[1]], capture_output=True, text=True, encoding='utf8', timeout=12)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError('Docker engine identity timed out') from error
            if result.returncode:
                raise RuntimeError('Docker engine identity could not be read')
            return result.stdout.strip()
    return command('info','--format','{{.ID}}')


async def docker(*args):
    return await finish_before_cancel(asyncio.to_thread(command, *args))


@dataclass(frozen=True)
class Runtime:
    deployment: UUID
    image: str
    engine: str

    @property
    def host_id(self):
        return 'docker:' + sha256(f'{self.deployment.hex}:{self.engine}:{self.image}'.encode()).hexdigest()


async def configured(settings):
    if not settings.acp_docker_image:
        return None
    image = await docker('image','inspect','--format','{{.Id}}',settings.acp_docker_image)
    engine = await finish_before_cancel(asyncio.to_thread(engine_identity))
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image) or not engine or len(engine) > 120:
        raise ValueError('Docker runtime identity is unavailable')
    return Runtime(UUID(settings.acp_docker_deployment), image, engine)


def proof(value, turn):
    expected = {'schema':SCHEMA,'turn':turn.id,'thread':turn.thread_id,
                'user':turn.created_by,'runtime':value.get('runtime')}
    if value != expected or not re.fullmatch(r'docker:[0-9a-f]{64}',expected['runtime'] or ''):
        raise ValueError('Invalid Docker ACP ownership receipt')
    return expected


def verified(receipt, stop, turn):
    proof(receipt, turn)
    return (stop.get('schema') == SCHEMA and stop.get('origin') == receipt
            and stop.get('verified') is True and stop.get('scope_empty') is True)


async def recover(turn, thread, settings):
    """Close only the Docker scope of an already verified, exited API owner."""
    runtime = await configured(settings)
    if runtime is None or runtime.host_id != thread.host_id or turn.created_by != thread.created_by:
        raise ValueError('The original Docker ACP runtime is unavailable')
    if turn.process_json is None:
        if turn.phase != 'admitted':
            raise ValueError('Started Docker ACP scope has no launch receipt')
        # Admission has no Docker side effects before the receipt is committed.
        return None
    receipt = proof(json.loads(turn.process_json), turn)
    if receipt['runtime'] != runtime.host_id:
        raise ValueError('Docker ACP scope belongs to a different runtime')
    input_dir = acp_case_files.directory(settings, turn.created_by, turn.thread_id, turn.id)
    cleanup = Transport(runtime, thread, turn.id, None, {'model':None}, None, input_dir=input_dir)
    cleanup.registered = True
    return await cleanup.close('owner_exited')


def binding(source, target, *, readonly=False):
    name = source.resolve(strict=True).as_posix()
    if any(char in name for char in ',\r\n'):
        raise ValueError('Unsupported Docker mount path')
    return f'type=bind,source={name},target={target}' + (',readonly' if readonly else '')


def entry(image, mode):
    launcher = ['--entrypoint','python',image,'-I','-u','/vf-runtime/entry.py',mode]
    if get_settings().acp_docker_entrypoints_in_image:
        return launcher
    worker = binding(ROOT/'docker_acp_entry.py', '/vf-runtime/entry.py', readonly=True)
    config = binding(ROOT/'acp_configuration.py', '/vf-runtime/acp_configuration.py', readonly=True)
    return ['--mount',worker,'--mount',config,*launcher]


class ScopeTransport:
    """Owned Docker relay and worker with a shared bounded stdio lifecycle."""

    def __init__(self, runtime, scope, label, receipt, model, key, *, max_output_bytes):
        self.runtime, self.scope, self.label, self.receipt = runtime, scope, label, receipt
        self.model, self.key = model, key
        self.capability = secrets.token_hex(32)
        self.volume = self.scope+'-relay'
        self.volume_mount = f'type=volume,source={self.volume},target=/relay,volume-nocopy'
        self.processes = {}
        self.stopping = None
        self.output = 0
        self.stream = None
        self.registered = False
        self.max_output_bytes = max_output_bytes
        self.resource_limit_reason = None
        self.container_ids = None
        self.worker_id = None
        self.resource_usage = None

    async def admit(self, callback):
        async def register():
            await callback(self.receipt)
            self.registered = True
        await finish_before_cancel(register())

    async def attach(self, ident, config, ready):
        proc = await asyncio.create_subprocess_exec(get_settings().docker_cli_client,'start','-ai',ident,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=MAX_LINE+1)
        self.processes[ident] = proc
        proc.stdin.write(json.dumps(config).encode('utf8')+b'\n')
        await proc.stdin.drain()
        if (await asyncio.wait_for(proc.stdout.readline(), 15)).decode().strip() != ready:
            raise RuntimeError('Docker CLI entrypoint did not announce readiness')
        return proc

    async def create_containers(self, state_mount, mounts, mode):
        await docker('volume','create','--driver','local','--label',self.label,self.volume)
        await self.verify_volume()
        await docker('run','--rm','--pull=never','--network','none','--user','0:0','--read-only','--cap-drop','ALL',
                     '--cap-add','CHOWN','--label',self.label,'--mount',self.volume_mount,
                     '--entrypoint','chown',self.runtime.image,'65534:65534','/relay')
        gateway = await docker('create','--interactive','--name',self.scope+'-relay','--label',self.label,
            '--pull=never','--network','bridge','--user','65534:65534','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges=true','--pids-limit','64','--memory','192m',
            '--mount',self.volume_mount,*entry(self.runtime.image,'relay'))
        worker = await docker('create','--interactive','--name',self.scope+'-worker','--label',self.label,
            *container_flags(),'--mount',state_mount,'--mount',self.volume_mount+',readonly',
            *mounts, *entry(self.runtime.image,mode))
        return gateway, worker

    async def attach_pair(self, gateway, worker, config, ready, *, wire='chat'):
        await self.attach(gateway, {'capability':self.capability,'key':self.key,'model':self.model,'wire':wire},
                          'VF_ACP_RELAY_READY')
        self.key = None
        self.stream = await self.attach(worker, {'capability':self.capability,'model':self.model,**config}, ready)
        self.worker_id = worker

    async def write(self, data):
        if not self.stream or self.stream.stdin.is_closing():
            raise EOFError('Docker CLI input ended')
        self.stream.stdin.write(data)
        await asyncio.wait_for(self.stream.stdin.drain(), 10)

    async def readline(self):
        data = await self.stream.stdout.readline()
        self.output += len(data)
        if self.max_output_bytes and self.output > self.max_output_bytes:
            self.resource_limit_reason = 'log_limit'
            raise ValueError('Docker CLI output exceeds its limit')
        if not data:
            raise EOFError('Docker CLI output ended')
        return data

    async def finish_input(self):
        if self.stream and not self.stream.stdin.is_closing():
            try:
                if self.worker_id is not None:
                    self.resource_usage = await docker_cli_usage.sample(self.worker_id)
            finally:
                self.stream.stdin.close()

    async def verify_volume(self):
        records = json.loads(await docker('volume','inspect',self.volume))
        if len(records) != 1:
            raise ValueError('Invalid Docker relay volume receipt')
        record = records[0]
        if (record.get('Name') != self.volume or record.get('Driver') != 'local' or record.get('Options')
                or record.get('Labels') != dict([self.label.split('=',1)])):
            raise ValueError('Docker model relay volume belongs to another execution')

    async def containers(self):
        raw = await docker('ps','-a','--no-trunc','--filter','label='+self.label,'--format','{{.ID}} {{.Names}}')
        selected = {}
        for line in raw.splitlines():
            ident,name = line.split(' ',1)
            if name not in {self.scope+'-worker',self.scope+'-relay'} or not re.fullmatch(r'[0-9a-f]{64}',ident):
                raise ValueError('Docker container set does not match registered ownership')
            selected[name.rsplit('-',1)[1]] = ident
        return selected

    async def _stop(self, reason):
        if not self.registered:
            return None
        containers = await self.containers()
        if self.container_ids is not None and any(self.container_ids.get(role) != ident
                                                  for role,ident in containers.items()):
            raise ValueError('Docker cleanup differs from the registered container IDs')
        if reason == 'owner_exited' and 'worker' in containers:
            self.resource_usage = await docker_cli_usage.sample(containers['worker'])
        results = {}
        for role,ident in sorted(containers.items(), reverse=True):
            if role == 'worker' and self.stream is not None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stream.wait(), 4)
            await docker('stop','--time','1',ident)
            state = json.loads(await docker('inspect','--format','{{json .State}}',ident))
            if state['Running'] or state['Status'] not in {'exited','created','dead'}:
                raise ValueError('Docker still reports an active CLI scope')
            results[role] = {'id':ident,'exit_code':state['ExitCode']}
        events = []
        if 'relay' in containers:
            raw = await docker('logs',containers['relay'])
            events = [json.loads(line) for line in raw.splitlines() if line.startswith('{')]
        for ident in containers.values():
            await docker('rm',ident)
        for proc in self.processes.values():
            proc.stdin.close()
            await asyncio.wait_for(proc.stdout.read(), 8)
            await asyncio.wait_for(proc.wait(), 8)
        volumes = await docker('volume','ls','--filter','label='+self.label,'--format','{{.Name}}')
        if volumes:
            if volumes != self.volume:
                raise ValueError('Multiple Docker relay volumes share an execution label')
            await self.verify_volume()
            await docker('volume','rm',self.volume)
        if await self.containers():
            raise ValueError('Docker ACP containers reappeared after stopping')
        await self.clean_scope()
        evidence = {'schema':self.receipt['schema'],'origin':self.receipt,'reason':reason,'verified':True,'scope_empty':True,
                'containers':results,'provider':'api.deepseek.com','model':self.model,'upstream':events}
        if self.resource_usage is not None and self.resource_usage['container_id'] == containers.get('worker'):
            evidence['resource_usage'] = self.resource_usage
        return evidence

    async def clean_scope(self):
        pass

    async def close(self, reason='cancelled'):
        self.key = None
        if self.stopping is None:
            self.stopping = asyncio.create_task(self._stop(reason))
        return await finish_before_cancel(self.stopping)


class Transport(ScopeTransport):
    def __init__(self, runtime, thread, turn_id, cwd, credential, key, *, input_dir=None, inputs=None):
        origin = {'schema':SCHEMA,'runtime':runtime.host_id,'turn':turn_id,'thread':thread.id,'user':thread.created_by}
        identifier = UUID(turn_id).hex
        super().__init__(runtime,'vf-acp-'+identifier,f'{LABEL}={runtime.deployment.hex}:{identifier}',
                        origin,credential['model'],key,max_output_bytes=32*1024**2)
        self.thread, self.cwd = thread, cwd
        self.input_dir, self.inputs = input_dir, inputs or {}

    async def prepare(self):
        if os.name == 'posix' and self.thread.allow_drafts:
            # Only the workspace is mounted. Its parent restricts host-side access.
            self.cwd.parent.chmod(0o700)
            self.cwd.chmod(0o777)
        mounts = ['--mount',binding(self.cwd, '/workspace', readonly=not self.thread.allow_drafts)]
        if self.inputs:
            await finish_before_cancel(asyncio.to_thread(acp_case_files.stage, self.input_dir, self.inputs))
            anchor = self.cwd/'case'
            if anchor.resolve() != anchor:
                raise ValueError('ACP case mount was redirected')
            anchor.mkdir(exist_ok=True)
            mounts += ['--mount',binding(self.input_dir, acp_case_files.MOUNT, readonly=True)]
        thread_id = UUID(self.thread.id)
        state = StateVolume(self.runtime.deployment,self.thread.created_by,thread_id,
                            uuid5(self.runtime.deployment,thread_id.hex))
        if self.thread.native_id is None:
            await finish_before_cancel(asyncio.to_thread(state.provision,command,self.runtime.image))
        state_mount = await finish_before_cancel(asyncio.to_thread(state.mount,command))
        return await self.create_containers(state_mount, mounts, 'acp')

    async def start(self, *, before_release=None, before_start=None):
        await self.admit(before_release)
        gateway, worker = await finish_before_cancel(self.prepare())
        await before_start(self.receipt)
        await self.attach_pair(gateway, worker, {'harness':self.thread.harness,'drafts':self.thread.allow_drafts,
            'contract':self.thread.contract_text is not None,'case_inputs':bool(self.inputs)}, 'VF_ACP_WORKER_READY')

    async def clean_scope(self):
        if self.input_dir is not None:
            await finish_before_cancel(asyncio.to_thread(acp_case_files.clean, self.input_dir))
