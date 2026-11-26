"""Docker process bindings for the platform Codex JSON-RPC lifecycle."""
import asyncio
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import re
from uuid import UUID, uuid5

from . import docker_acp, docker_cli_usage
from .async_ops import finish_before_cancel
from .codex_images import DOCKER_INPUTS
from .docker_cli_state import StateVolume
from .owned_stdio import packed


SCHEMA = 'vibeflow.codex.docker.v1'
LABEL = 'vibeflow.codex.execution'
ENTRY = Path(__file__).with_name('docker_acp_entry.py')
CONFIGURATION = Path(__file__).with_name('acp_configuration.py')
CWD = '/workspace'
JOURNAL = {'schema': SCHEMA, 'launch': None, 'containers': None}


def runtime_value(runtime):
    return {**asdict(runtime), 'deployment': str(runtime.deployment)}


def runtime_from(value):
    if (not isinstance(value, dict) or set(value) != {'deployment','image','engine'}
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['image'])
            or not re.fullmatch(r'[a-zA-Z0-9_:-]{1,120}', value['engine'])):
        raise ValueError('Invalid Codex Docker runtime')
    deployment = UUID(value['deployment'])
    if str(deployment) != value['deployment']:
        raise ValueError('Invalid Codex Docker deployment')
    return docker_acp.Runtime(deployment,value['image'],value['engine'])


def launch(runtime, identifier, user_id, nonce):
    return {'schema':SCHEMA,'runtime':runtime.host_id,'session':identifier,'user':user_id,'nonce':nonce}


def state(runtime, user_id, credential):
    # Forks and resumes use this same account and credential revision.
    workspace = uuid5(runtime.deployment, f'codex:{user_id}:{credential["revision"]}')
    return StateVolume(runtime.deployment,user_id,workspace,uuid5(runtime.deployment,workspace.hex))


def ids(value, *, partial=False):
    if (not isinstance(value, dict) or not set(value) <= {'worker','relay'}
            or (not partial and set(value) != {'worker','relay'})
            or len(set(value.values())) != len(value)
            or any(not re.fullmatch('[0-9a-f]{64}', ident) for ident in value.values())):
        raise ValueError('Invalid Codex Docker container identities')
    return value


def validate_stop(value, origin, committed):
    if (not isinstance(value,dict) or set(value)-{'resource_usage'} != {'schema','origin','reason','verified','scope_empty',
            'containers','provider','model','upstream'} or value['schema'] != SCHEMA or value['origin'] != origin
            or value['reason'] not in {'root_exit','owner_exited'}
            or value['verified'] is not True or value['scope_empty'] is not True
            or value['provider'] != 'api.deepseek.com'):
        raise ValueError('Invalid Codex Docker exit evidence')
    containers = value['containers']
    if not isinstance(containers,dict):
        raise ValueError('Invalid Codex Docker stop containers')
    stopped = ids({role:item['id'] for role,item in containers.items()}, partial=True)
    if any(set(item) != {'id','exit_code'} or type(item['exit_code']) is not int for item in containers.values()):
        raise ValueError('Invalid Codex Docker stop status')
    if committed is not None and any(committed.get(role) != ident for role,ident in stopped.items()):
        raise ValueError('Codex Docker stop crosses its registered container IDs')
    if value['reason'] == 'root_exit' and committed is not None and committed != stopped:
        raise ValueError('Codex Docker normal exit lacks a registered container status')
    if 'resource_usage' in value:
        docker_cli_usage.validate(value['resource_usage'],stopped.get('worker'))


def launch_profile(argv, nonce, runtime):
    return {'argv':argv,'cwd':CWD,'nonce':nonce,'docker_runtime':runtime_value(runtime)}


class Transport(docker_acp.ScopeTransport):
    def __init__(self, runtime, identifier, user_id, nonce, cwd, credential, model, key, *,
                 max_output_bytes=0, writable=False, inputs=None):
        origin = launch(runtime,identifier,user_id,nonce)
        scope_id = UUID(identifier).hex
        super().__init__(runtime,'vf-codex-'+scope_id,f'{LABEL}={runtime.deployment.hex}:{scope_id}',
                         origin,model,key,max_output_bytes=max_output_bytes)
        self.cwd = cwd
        self.credential = credential
        self.profile = launch_profile([str(ENTRY)],nonce,runtime)
        self.profile_sha256 = sha256(packed(self.profile)).hexdigest()
        self.code_hashes = {path:sha256(path.read_bytes()).hexdigest() for path in (ENTRY,CONFIGURATION)}
        self.container_ids = None
        self.stop_result = None
        self.env = {}
        self.writable = writable
        self.inputs = inputs

    @property
    def proc(self):
        return self.stream

    @property
    def eof(self):
        return self.stream is None or self.stream.stdout.at_eof()

    @property
    def exit_code(self):
        worker = self.stop_result['containers'].get('worker') if self.stop_result else None
        return worker['exit_code'] if worker else None

    def check_code(self):
        if any(sha256(path.read_bytes()).hexdigest() != value for path,value in self.code_hashes.items()):
            raise ValueError('Codex Docker entrypoint changed after admission')

    async def prepare(self):
        volume = state(self.runtime,self.receipt['user'],self.credential)
        def prepare_state():
            names = docker_acp.command('volume','ls','--filter','name='+volume.name,'--format','{{.Name}}').splitlines()
            if volume.name not in names:
                volume.provision(docker_acp.command,self.runtime.image)
            return volume.mount(docker_acp.command)
        mounted = await finish_before_cancel(asyncio.to_thread(prepare_state))
        mounts = ['--mount',docker_acp.binding(self.cwd,CWD,readonly=not self.writable)]
        if self.inputs is not None:
            mounts.extend(('--mount',docker_acp.binding(self.inputs,DOCKER_INPUTS,readonly=True)))
        return await self.create_containers(mounted,mounts,'codex')

    async def start(self, *, before_release=None, before_start=None, after_prepare=None):
        await self.admit(before_release)
        gateway, worker = await finish_before_cancel(self.prepare())
        self.container_ids = ids({'worker':worker,'relay':gateway})
        if await self.containers() != self.container_ids:
            raise ValueError('Codex Docker process scope differs from its created IDs')
        await before_start(self.container_ids)
        await self.attach_pair(gateway,worker,{'writable':self.writable,'inputs':self.inputs is not None},
                               'VF_CODEX_WORKER_READY',wire='responses')

    async def close(self, reason='root_exit'):
        await self.finish_input()
        self.stop_result = await super().close(reason)
        return self.stop_result
