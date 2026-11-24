"""Durable host CLI sessions with short authorization transactions."""
import asyncio
from contextlib import asynccontextmanager,nullcontext
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time
import tomllib
import uuid

from fastapi import HTTPException

from .. import storage
from ..config import get_settings
from ..db import SessionLocal
from ..models import CaseEventCounter, Conversation, NativeCliSession, User
from . import codex_docker, codex_docker_stage, codex_records as records, docker_acp
from . import codex_credentials as credentials
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, coordinate
from .codex_permissions import READ_ONLY, THREAD_POLICIES
from .cli_discovery import discover_cli
from .codex_environment import OS_ENVIRONMENT, create_isolated_codex_environment
from .codex_stdio import CodexRpcError, CodexStdioClient
from .codex_projection import Secrets
from .owned_stdio import BRIDGE, IDENTITY, NATIVE_PROBE, NATIVE_STATE
from .runtime_config import get_runtime_setting
from .supervisor import register, unregister
from .workflow_state import ExecutionConflict
from . import request_deadline

SESSIONS = {}
HEARTBEAT_INTERVAL = 5
IDLE_SECONDS = 900
PRIVATE_ENVIRONMENT_BYTES = 64 * 1024


def sha_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def command(credential=None):
    found = discover_cli('codex')
    if found is None:
        raise HTTPException(503, '未发现服务器上的 Codex CLI')
    program = str(Path(found['path']).resolve())
    if Path(program).suffix.lower() in {'.cmd', '.bat'}:
        raise HTTPException(503, '当前所属 CLI 适配需要原生可执行程序，请配置 Codex exe')
    argv = [program, '-c', 'cli_auth_credentials_store="file"']
    windows_sandbox = get_settings().codex_windows_sandbox
    if windows_sandbox is not None:
        argv.extend(['-c', f'windows.sandbox="{windows_sandbox}"'])
    if credential is not None:
        for key, value in credentials.overrides(credential).items():
            argv.extend(['-c', key + '=' + credentials.toml(value)])
    return [*argv, 'app-server', '--listen', 'stdio://']


def windows_sandbox_override(argv):
    for option in ('elevated', 'unelevated'):
        if f'windows.sandbox="{option}"' in argv:
            return option
    return None


def _windows_setup_configuration(actual, expected, mode):
    if mode is None:
        return False
    try:
        settings = tomllib.loads(actual)
    except tomllib.TOMLDecodeError:
        return False
    return settings.pop('windows', None) == {'sandbox': mode} and settings == tomllib.loads(expected)


def native_command(credential=None, *, distro=None):
    configured = get_settings().codex_wsl_path
    if configured:
        program = Path(configured).resolve()
    else:
        found = discover_cli('codex')
        if found is None:
            raise HTTPException(503, '未配置 Linux Codex 程序')
        program = (Path(found['path']).resolve().parent.parent / 'linux-x86_64' / 'codex').resolve()
    if not program.is_file() or program.is_relative_to(Path(get_settings().storage_fs_root).resolve()):
        raise HTTPException(503, 'Linux Codex 程序不可用，或位于公开算例存储中')
    with program.open('rb') as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00':
        raise HTTPException(503, 'WSL 后端需要 Linux x86_64 原生 Codex 程序')
    from .codex_wsl_state import mounted_path
    argv = [mounted_path(program, distro or get_runtime_setting('wsl_distro', 'vibeflow-linux')), '-c', 'cli_auth_credentials_store="file"']
    if credential is not None:
        for key, value in credentials.overrides(credential).items():
            argv.extend(['-c', key + '=' + credentials.toml(value)])
    return str(program), [*argv, 'app-server', '--listen', 'stdio://']


def private_environment(user_id, identifier, credential=None, *, runtime='windows', distro=None):
    if type(user_id) is not int or user_id<=0 or not isinstance(identifier,str) or str(uuid.UUID(identifier))!=identifier:
        raise ValueError('Invalid private CLI owner or session identifier')
    from .node_storage_budget import reserve
    # Covers the default config, replacement config and owner metadata. The
    # writer below validates this bound before creating account/session state.
    with reserve(PRIVATE_ENVIRONMENT_BYTES,private=True):
        return _private_environment(user_id,identifier,credential,runtime=runtime,distro=distro)


def _private_environment(user_id, identifier, credential=None, *, runtime='windows', distro=None):
    import os
    settings = get_settings()
    if runtime == 'wsl' and not settings.codex_wsl_state_root:
        raise HTTPException(503, '请由部署管理员配置 Linux 原生私有目录 VIBEFLOW_CODEX_WSL_STATE_ROOT')
    from .cli_private_paths import root as private_root
    root = private_root(settings)
    if runtime not in {'windows', 'wsl', 'docker'}:
        raise ValueError('Invalid private CLI runtime')
    account = root / 'users' / str(user_id) / runtime
    expected = {'schema': 'vibeflow.cli-account.v1', 'user_id': user_id, 'runtime': runtime}
    if runtime == 'wsl':
        import re
        if not isinstance(distro, str) or not re.fullmatch('[a-z0-9-]+', distro):
            raise ValueError('Invalid private CLI distribution')
        account = account / distro
        expected['distro'] = distro
    configuration = None
    if credential is not None:
        credentials.validate(credential)
        account = account / 'providers' / str(credential['profile_id']) / credential['revision']
        expected['credential'] = credential
        configuration = credentials.config(credential)
    if not account.resolve().is_relative_to(root):
        raise ValueError('CLI account directory escaped private storage')
    home = account / 'home'
    marker = account / 'owner.json'
    predicted = 'cli_auth_credentials_store = "file"\n' + (configuration or '') + json.dumps(expected)
    if len(predicted.replace('\n',os.linesep).encode('utf8'))>PRIVATE_ENVIRONMENT_BYTES:
        raise ValueError('CLI private initialization exceeds its reserved bytes')
    if not home.exists():
        environment = create_isolated_codex_environment(home)
        if configuration is not None:
            (home / 'config.toml').write_text(configuration, encoding='utf8')
        # A concurrent initializer may fail before the marker exists; it must
        # retry explicitly, never adopt an unmarked credential directory.
        with marker.open('x', encoding='utf8') as stream:
            json.dump(expected, stream)
    else:
        if home.is_symlink() or not marker.is_file() or json.loads(marker.read_text(encoding='utf8')) != expected:
            raise ValueError('CLI account directory has no matching user ownership')
        environment = {key: value for key, value in os.environ.items() if key.upper() in OS_ENVIRONMENT}
        environment['CODEX_HOME'] = str(home)
    if not (home / 'config.toml').is_file():
        raise ValueError('CLI account configuration is unavailable')
    if configuration is not None:
        actual = (home / 'config.toml').read_text(encoding='utf8')
        if actual != configuration and not (
            runtime == 'windows' and _windows_setup_configuration(actual, configuration, settings.codex_windows_sandbox)
        ):
            raise ValueError('Hosted CLI configuration changed; refusing credential injection')
    # Keep CLI user configuration, shell profiles and temporary files out of
    # the host operator's personal directories. This is logical account state,
    # not a substitute for the planned OS/container isolation.
    roaming, local, temporary = account / 'appdata/roaming', account / 'appdata/local', account / 'tmp'
    for directory in (roaming, local, temporary):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(HOME=str(account), USERPROFILE=str(account), APPDATA=str(roaming),
                       LOCALAPPDATA=str(local), TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary), NO_PROXY='localhost,127.0.0.1,::1')
    native_state = None
    if runtime == 'wsl':
        from .codex_wsl_state import selection, windows_home
        native_state = selection(settings.codex_wsl_state_root,
            account.relative_to(root).as_posix(), (home / 'config.toml').read_text(encoding='utf8'), expected)
        native_home = windows_home(native_state, distro)
        environment.update(CODEX_HOME=str(native_home), HOME=str(native_home.parent), TMPDIR=str(native_home.parent / 'tmp'))
    # New state remains attributable even if startup fails before the DB owner
    # commits. Existing flat session directories are resolved from DB history.
    workspace = root / 'users' / str(user_id) / 'sessions' / identifier / 'workspace'
    workspace.mkdir(parents=True, exist_ok=False)
    return environment, workspace, native_state


async def authorize(db, case_id, conversation_id, user_id, *, expected=None):
    from ..routers.workspaces import get_owned_case
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or user.status != 'active':
        raise HTTPException(403, 'CLI 用户已不可用')
    case = await get_owned_case(case_id, user, db) if case_id is not None else None
    conversation = await db.get(Conversation, conversation_id, populate_existing=True) if conversation_id is not None else None
    if conversation_id is not None and (conversation is None or conversation.case_id != case_id):
        raise HTTPException(404, 'CLI 会话不属于该算例')
    permission = conversation.permission_mode if conversation is not None else 'ask'
    if expected is not None and ((case.s3_prefix if case is not None else None) != expected['source_prefix']
            or permission != expected['permission_mode']
            or ('model' in expected and (conversation.model if conversation is not None else '') != expected['model'])):
        raise CaseWriteConflict('input_stale', 'CLI 算例来源或权限模式已变化')
    if expected is not None and 'credential' in expected:
        if (conversation.provider_profile_id if conversation is not None else None) != expected['conversation_provider_id']:
            raise CaseWriteConflict('input_stale', '会话的 API 配置选择已变化')
        if expected['credential'] is not None:
            await credentials.check(db, expected['credential'], user_id)
    if expected is not None and expected['runtime'] == 'wsl' and expected['distro'] != get_runtime_setting('wsl_distro', 'vibeflow-linux'):
        raise CaseWriteConflict('input_stale', 'CLI 的 WSL 发行版配置已变化')
    if expected is not None and expected['runtime'] == 'wsl' and expected['state_root'] != get_settings().codex_wsl_state_root:
        raise CaseWriteConflict('input_stale', 'CLI 的 Linux 私有目录配置已变化')
    return case, permission


@asynccontextmanager
async def control_case(db,case,*,allow_deleted=False):
    """Serialize CLI SQL records without writing a remote file-recovery fence."""
    from .durable_events import lock_case_execution
    from .case_write import assert_case_available
    from .storage_operations import admit_locked
    from .case_validation import assert_writable_locked
    await lock_case_execution(db,case.id)
    await db.refresh(case)
    if not allow_deleted:assert_case_available(case)
    await admit_locked(db,case.id)
    await assert_writable_locked(db,case.id)
    yield
    await db.flush()


class Session:
    def __init__(self, case_id, conversation_id, user_id, *, provider_profile_id=None,
                 provider_wire_api=None, runtime='windows', fork_only=False):
        if runtime not in {'windows', 'wsl', 'docker'}:
            raise ValueError('Invalid CLI runtime')
        self.runtime = runtime
        self.distro = get_runtime_setting('wsl_distro', 'vibeflow-linux') if runtime == 'wsl' else None
        self.id = str(uuid.uuid4())
        self.case_id = case_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.provider_profile_id = provider_profile_id
        self.provider_wire_api = provider_wire_api
        self.fork_only = fork_only
        self.fork_workspace = None
        self.credential = None
        self.secret = None
        self.owner = None
        self.selected = None
        self.client = None
        self.stage = None
        self.start_task = None
        self.start_finished = asyncio.Event()
        self.starting = False
        self.retire_task = None
        self.close_task = None
        self.close_error = None
        self.heartbeat_task = None
        self.watch_task = None
        self.storage_task = None
        self.operation_lock = asyncio.Lock()
        self.last_activity = time.monotonic()
        self.persisted = False
        self.cleanup_verified = None
        self.error = None
        self.stopping = False
        self.turn_execution = None
        self.thread_model = None
        self.resumed_approved_plan = False
        self.startup_guard = None
        self.on_registered = None
        self.native_state = None
        self.docker_runtime = None
        self.docker_stage = None
        self.docker_stage_source = None
        self.scope_owner = None
        self.native_scope = None
        self.scope_deadline = None
        settings = get_settings()
        self.lifetime_seconds = settings.codex_session_lifetime_seconds
        self.max_output_bytes = settings.codex_session_max_output_bytes

    @property
    def resource_error(self):
        if self.client is None:
            return None
        return {'timeout': 'CliSessionTimeout', 'log_limit': 'CliSessionOutputLimit'}.get(
            self.client.transport.resource_limit_reason)

    @asynccontextmanager
    async def access(self,*,storage_capacity=False,private_bytes=0,private_capacity=False,prepare_quota=False,
                     initialization_credit=False):
        async with SessionLocal() as db:
            case, _ = await authorize(db, self.case_id, self.conversation_id, self.user_id, expected=self.selected)
            from .owner_storage_budget import waiting,current
            from .case_read_capacity import admitted
            capacity = (waiting(db,self.user_id) if private_capacity else
                        admitted(db,case) if storage_capacity and case is not None else nullcontext())
            async with capacity:
                scope=current() if storage_capacity or private_capacity else None
                if scope is not None and scope.ticket['user_id']==self.user_id and private_bytes:
                    scope.reserve_future(private_bytes)
                if prepare_quota:
                    from .cli_user_capacity import lock_startup_inventory
                    inventory = await lock_startup_inventory(db, self.user_id, scope.inventory if scope else None)
                    case, _ = await authorize(db, self.case_id, self.conversation_id, self.user_id, expected=self.selected)
                    admission = db, inventory
                else:
                    admission = db
                if initialization_credit or (private_capacity and not prepare_quota):
                    from .storage_admission import lock_owner
                    await lock_owner(db,self.user_id,wait=True)
                if case is not None:
                    # Lifecycle records do not mutate case files. Only the
                    # staging snapshot and image writes need file recovery.
                    async with coordinate(db, case) if storage_capacity else control_case(db,case):
                        if self.startup_guard is not None:
                            await self.startup_guard(db)
                        await authorize(db, self.case_id, self.conversation_id, self.user_id, expected=self.selected)
                        if self.selected is not None:
                            cwd = self.fork_workspace or storage._fs_path(self.stage or case.s3_prefix, '.')
                            if str(cwd.resolve()) != self.selected['cwd']:
                                raise CaseWriteConflict('input_stale', 'CLI 工作目录已变化')
                        yield admission
                else:
                    yield admission
                await finish_before_cancel(db.commit())

    def verify_code(self):
        if 'native_management' in self.selected:
            from .cli_scope_lifecycle import check_code
            check_code(self.selected)
        if sha_file(self.selected['program']) != self.selected['program_sha256']:
            raise ValueError('CLI executable changed after selection')
        if self.native_state is None or self.client.transport.native_state_prepared:
            if sha_file(Path(self.selected['auth_home']) / 'config.toml') != self.selected['auth_config_sha256']:
                raise ValueError('CLI configuration changed after selection')
        self.client.transport.check_code()

    def ephemeral_secrets(self):
        capability = self.client.transport.capability if self.docker_runtime and self.client else None
        return tuple(value for value in (self.secret,capability) if value)

    def clean(self, value):
        return Secrets(self.selected['auth_home'], ephemeral=self.ephemeral_secrets()).clean(value)

    def check_identifier(self, value):
        if isinstance(value,str) and any(secret in value for secret in self.ephemeral_secrets()):
            raise ValueError('Native identifier contains a provider credential')

    def check_startup(self):
        deadline = request_deadline.current()
        if deadline is not None:
            deadline.check()
        if self.stopping:
            raise asyncio.CancelledError

    async def prepare(self):
        self.check_startup()
        # Reject unauthorized users before creating any private state.
        async with self.access() as db:
            self.credential, self.secret = await credentials.select(db, self.provider_profile_id, self.user_id, self.provider_wire_api)
            conversation = await db.get(Conversation, self.conversation_id) if self.conversation_id is not None else None
            conversation_provider_id = conversation.provider_profile_id if conversation is not None else None
            permission = conversation.permission_mode if conversation is not None else 'ask'
        self.check_startup()
        if self.runtime == 'wsl':
            program, argv = await finish_before_cancel(asyncio.to_thread(native_command, self.credential, distro=self.distro))
            self.check_startup()
        elif self.runtime == 'docker':
            if (self.credential is None or self.credential['owner_id'] != self.user_id
                    or self.credential['provider'] != 'deepseek'
                    or self.credential['base_url'] not in {'https://api.deepseek.com','https://api.deepseek.com/v1'}):
                raise HTTPException(422,'Docker Codex 需要本人配置的官方 DeepSeek Responses API')
            self.docker_runtime = await docker_acp.configured(get_settings())
            if self.docker_runtime is None:
                raise HTTPException(503,'Docker CLI 执行环境尚未配置')
            program = str(codex_docker.ENTRY.resolve())
            argv = [program]
        else:
            argv = await finish_before_cancel(asyncio.to_thread(command, self.credential))
            self.check_startup()
            program = argv[0]
        from .cli_user_capacity import initialize
        from .node_storage_budget import allocation
        # Keep the disk claim through the bounded write and SQL commit. A slow
        # node inventory must finish before the case/account write lock begins.
        async with allocation(PRIVATE_ENVIRONMENT_BYTES, private=True):
            self.check_startup()
            async with self.access(private_capacity=True,private_bytes=PRIVATE_ENVIRONMENT_BYTES,prepare_quota=True) as (db, inventory):
                self.check_startup()
                await records.admit_capacity(db, self.user_id)
                environment,workspace,self.native_state=await initialize(db,self.user_id,self.id,self.credential,
                    runtime=self.runtime,distro=self.distro,node_admitted=True,inventory=inventory)
        self.check_startup()
        if self.secret is not None and self.runtime != 'docker':
            environment[credentials.TOKEN_ENV] = self.secret
        self.input_root = workspace.parent / 'inputs'
        docker_inputs = None
        if self.runtime == 'docker' and self.case_id is not None:
            from .codex_images import docker_input_root
            docker_inputs = await finish_before_cancel(asyncio.to_thread(
                docker_input_root,environment['CODEX_HOME'],self.case_id))
            self.input_root = docker_inputs
        program_hash = await finish_before_cancel(asyncio.to_thread(sha_file, program))
        self.check_startup()
        staging_binding = None
        remote_case = self.case_id is not None and storage._use_s3()
        if self.runtime == 'docker' or remote_case and self.fork_only:
            # thread/fork only copies native history. Turns on the new
            # conversation will open a separate session with a fresh snapshot.
            self.fork_workspace = workspace
            if self.runtime == 'docker' and permission not in READ_ONLY:
                await self.prepare_docker_stage()
        elif remote_case:
            # Freeze the source and control sequence under a short transaction.
            # Cooperating file writes publish events under this same case lock;
            # any intervening event conservatively requires a new snapshot.
            async with self.access(storage_capacity=True) as db:
                self.check_startup()
                case, permission = await authorize(db, self.case_id, self.conversation_id, self.user_id)
                conversation = await db.get(Conversation, self.conversation_id) if self.conversation_id is not None else None
                counter = await db.get(CaseEventCounter, case.id, populate_existing=True)
                staging_binding = (case.s3_prefix, permission,
                    conversation.model if conversation is not None else '',
                    conversation.provider_profile_id if conversation is not None else None, counter.next_seq)
            self.check_startup()
            def stage():
                # Capture the actual completed download before cancellation is
                # propagated, so retirement can remove this unused snapshot.
                self.stage = storage.prepare_job_staging(staging_binding[0], uuid.uuid4().int)
            await finish_before_cancel(asyncio.to_thread(stage))
            self.check_startup()
        if self.runtime=='wsl':
            from . import cli_scope_allocation as scopes
            async with self.access() as db:
                self.check_startup()
                self.scope_owner=await scopes.create(db,self.id,self.user_id,self.distro)
            self.check_startup()
            self.native_scope=await scopes.allocate(self.scope_owner,check_active=self.check_startup)
            self.check_startup()
        wsl_credit = self.runtime == 'wsl'
        # Native initialization writes only private local bytes. Its durable
        # credit uses the measured account quota and existing remote credits.
        async with self.access(private_capacity=not wsl_credit,private_bytes=64*1024,prepare_quota=wsl_credit) as admission:
            db, inventory = admission if wsl_credit else (admission, None)
            self.check_startup()
            case, permission = await authorize(db, self.case_id, self.conversation_id, self.user_id)
            conversation = await db.get(Conversation, self.conversation_id) if self.conversation_id is not None else None
            if (conversation.provider_profile_id if conversation is not None else None) != conversation_provider_id:
                raise CaseWriteConflict('input_stale', '会话的 API 配置选择已变化')
            if self.credential is not None:
                await credentials.check(db, self.credential, self.user_id)
            if staging_binding is not None:
                counter = await db.get(CaseEventCounter, case.id, populate_existing=True)
                current_binding = (case.s3_prefix, permission,
                    conversation.model if conversation is not None else '',
                    conversation.provider_profile_id if conversation is not None else None, counter.next_seq)
                if current_binding != staging_binding:
                    raise CaseWriteConflict('input_stale', 'CLI 下载期间算例或会话已变化，请重新启动')
            if case is not None:
                source = case.s3_prefix
                cwd = (self.fork_workspace or storage._fs_path(self.stage or source, '.')).resolve()
            else:
                source, cwd = None, workspace
            driver = None
            if self.docker_runtime:
                if (permission not in READ_ONLY) != (self.docker_stage is not None):
                    raise CaseWriteConflict('input_stale','Docker CLI 权限模式在准备期间发生变化')
                if self.docker_stage is not None:
                    counter = await db.get(CaseEventCounter,case.id,populate_existing=True)
                    if (source,permission,counter.next_seq) != self.docker_stage_source:
                        raise CaseWriteConflict('input_stale','Docker CLI 暂存期间算例已变化，请重新启动')
                model = (conversation.model if conversation is not None else '') or self.credential['default_model']
                if not model:
                    raise HTTPException(422,'请选择 Docker Codex 使用的模型')
                driver = codex_docker.Transport(self.docker_runtime,self.id,self.user_id,secrets.token_hex(32),
                    cwd,self.credential,model,self.secret,max_output_bytes=self.max_output_bytes,
                    writable=self.docker_stage is not None,inputs=docker_inputs)
            self.client = CodexStdioClient(argv, environment, cwd=cwd,
                before_release=self.register_root, before_start=self.register_bridge, distro=self.distro,
                native_state=self.native_state, after_prepare=self.state_prepared, native_scope=self.native_scope,
                lifetime_seconds=self.lifetime_seconds, max_output_bytes=self.max_output_bytes, transport=driver)
            transport = self.client.transport
            nonce = driver.receipt['nonce'] if driver else transport.owned.nonce
            transport_profile = ({'profile_sha256':transport.profile_sha256,
                                  'entry_sha256':driver.code_hashes[codex_docker.ENTRY],
                                  'configuration_sha256':driver.code_hashes[codex_docker.CONFIGURATION]} if driver else
                                 {'profile_sha256':transport.profile_sha256,
                                  'bridge_sha256':sha_file(BRIDGE),'identity_sha256':sha_file(IDENTITY)})
            self.selected = {'case_id': self.case_id, 'conversation_id': self.conversation_id, 'user_id': self.user_id,
                'runtime': self.runtime, 'program': program, 'program_sha256': program_hash, 'argv': argv,
                'cwd': str(cwd), 'auth_home': environment['CODEX_HOME'],
                'auth_config_sha256': (hashlib.sha256(self.native_state['configuration'].encode('utf8')).hexdigest()
                                       if self.native_state is not None else sha_file(Path(environment['CODEX_HOME']) / 'config.toml')),
                'source_prefix': source, 'staging_prefix': self.stage,
                'permission_mode': permission, 'nonce': nonce, 'transport':transport_profile}
            if driver:
                self.selected['docker_runtime'] = codex_docker.runtime_value(self.docker_runtime)
                if self.docker_stage is not None:
                    self.selected['docker_stage'] = self.docker_stage
                if self.lifetime_seconds:
                    self.scope_deadline = time.monotonic() + self.lifetime_seconds
            conversation = await db.get(Conversation, self.conversation_id) if self.conversation_id is not None else None
            self.selected['model'] = conversation.model if conversation is not None else ''
            if self.distro is not None:
                self.selected['distro'] = self.distro
                self.selected['state_root'] = get_settings().codex_wsl_state_root
                self.selected['transport']['native_probe_sha256'] = sha_file(NATIVE_PROBE)
                self.selected['transport']['native_state_sha256'] = sha_file(NATIVE_STATE)
                self.selected['native_state'] = self.native_state
                from .cli_scope_lifecycle import selection
                self.selected['native_management']=transport.native_management
                self.selected['scope_lifecycle']=selection(workspace)
            self.selected['credential'] = self.credential
            self.selected['conversation_provider_id'] = conversation_provider_id
            async def allocate():
                candidate = await records.create(self.id, self.selected, db=db,scope_owner=self.scope_owner)
                from .cli_initialization_capacity import reserve
                await reserve(db,candidate,self.selected,inventory=inventory)
                self.owner = candidate
                if self.on_registered is not None:
                    await self.on_registered(db, self)
            await finish_before_cancel(allocate())
        self.check_startup()
        if self.runtime=='wsl':
            from .cli_initialization_capacity import acquire_node
            await acquire_node(self.owner)
            self.check_startup()
            owned=self.client.transport.owned
            frozen=self.selected['scope_lifecycle']
            self.scope_deadline=time.monotonic()+frozen['max_seconds']-30
            await owned.prepare_guardian(frozen['guardian_root'],max_seconds=frozen['max_seconds'])
            self.check_startup()
            from .cli_scope_lifecycle import register as register_scope
            async with self.access() as db:
                self.check_startup()
                await register_scope(db,self.owner,'guardian',{'request':owned.guardian.request,'preparation':owned.guardian.prepared})
            owned.on_native_preflight=self.register_native_preflight
            self.check_startup()
        SESSIONS[self.id] = self
        register('codex', self.id, self.case_id, self.close)

    async def prepare_docker_stage(self):
        from .case_write import bounded_case_read

        # Public S3 reads intentionally wait behind an admitted native
        # startup. Snapshot admission uses the same owner path as WSL.
        async with self.access(storage_capacity=True) as db:
            case, permission = await authorize(db,self.case_id,self.conversation_id,self.user_id)
            if case is None or permission in READ_ONLY:
                raise CaseWriteConflict('input_stale','Docker CLI 写入需要当前算例与可写模式')
            prefix = case.s3_prefix
            counter = await db.get(CaseEventCounter,case.id,populate_existing=True)
            self.docker_stage_source = prefix,permission,counter.next_seq

        def create():
            # Retain the copy for cleanup even if the outer deadline expires
            # after the last byte is written.
            self.docker_stage = codex_docker_stage.prepare(prefix,self.user_id,self.id)
        await bounded_case_read(create,code='codex_stage',label='Codex 算例暂存')
        self.fork_workspace = codex_docker_stage.root(self.user_id,self.id)/'files'
        self.check_startup()

    async def finish_docker_stage(self, *, publish):
        if self.docker_stage is None:
            return
        if self.selected is None:
            await finish_before_cancel(asyncio.to_thread(codex_docker_stage.discard_unmounted,self.user_id,self.id))
            return
        turn = self.turn_execution
        if publish and turn is not None and turn.finalizing:
            from .conversation_execution import current
            async def authorized(db):
                case,_ = await authorize(db,self.case_id,self.conversation_id,self.user_id,expected=self.selected)
                await records.current(db,self.owner)
                await current(db,turn.owner,launch=False)
                return case
            await codex_docker_stage.publish(self.selected,self.id,turn.id,authorized)
        await finish_before_cancel(asyncio.to_thread(codex_docker_stage.discard,self.selected,self.id))

    async def register_native_preflight(self,value):
        self.check_startup()
        await finish_before_cancel(asyncio.to_thread(self.verify_code))
        async with self.access() as db:
            self.check_startup()
            from .cli_scope_lifecycle import register as register_scope
            await register_scope(db,self.owner,'preflight',value)

    async def register_root(self, root):
        self.check_startup()
        await finish_before_cancel(asyncio.to_thread(self.verify_code))
        self.check_startup()
        async with self.access() as db:
            self.check_startup()
            await records.register_root(db, self.owner, root, self.client.transport)

    async def register_bridge(self, _birth):
        self.check_startup()
        await finish_before_cancel(asyncio.to_thread(self.verify_code))
        self.check_startup()
        # The WSL initializer already has durable account and node credit.
        async with self.access(initialization_credit=self.runtime=='wsl') as db:
            self.check_startup()
            await records.register_bridge(db, self.owner, self.client.transport)
            if self.runtime=='wsl':
                from .cli_initialization_capacity import permit_initializer
                await permit_initializer(db,self.owner,self.client.transport)

    async def state_prepared(self):
        self.check_startup()
        # The Linux initializer has completed inside the registered bridge.
        # Recheck both frozen configuration and current access before the CLI
        # itself can start; a revoked startup must never cross this last gate.
        await finish_before_cancel(asyncio.to_thread(self.verify_code))
        self.check_startup()
        # The bytes already have private credit. Settle the verified
        # initializer before the post-start inventory observes actual usage.
        async with self.access(initialization_credit=True) as db:
            self.check_startup()
            from .cli_initialization_capacity import prepared
            await prepared(db,self.owner,self.client.transport)
        from .cli_initialization_capacity import closed
        await closed(self.user_id)
        self.check_startup()

    async def start(self):
        self.start_task = asyncio.current_task()
        self.start_finished.clear()
        self.starting = True
        deadline = None
        try:
            async with request_deadline.scope(request_deadline.CLI_STARTUP_SECONDS) as deadline:
                await self.prepare()
                self.check_startup()
                from .cli_runtime_storage import observe
                await observe(self.user_id)
                self.check_startup()
                self.heartbeat_task = asyncio.create_task(self.renew())
                await self.client.start()
                self.check_startup()
                await observe(self.user_id)
                self.check_startup()
                async with self.access() as db:
                    self.check_startup()
                    if self.client._failure is not None or self.client.transport.eof:
                        raise EOFError('CLI stream ended during initialization')
                    await records.activate(db, self.owner, self.clean(self.client.initialize_result))
                self.check_startup()
                self.watch_task = asyncio.create_task(self.watch())
                self.storage_task = asyncio.create_task(self.observe_storage())
                return self
        except BaseException as error:
            cancelled_by_close = isinstance(error, asyncio.CancelledError) and self.stopping
            failure = 'TimeoutError' if deadline is not None and deadline.expired else getattr(error,'code',None) or type(error).__name__
            if self.runtime == 'docker' and not cancelled_by_close:
                logging.getLogger(__name__).warning('Docker Codex startup stopped: session=%s reason=%s',self.id,failure)
            try:
                await finish_before_cancel(self.retire(self.close_error if cancelled_by_close else failure))
            except Exception as cleanup_error:
                error.add_note('CLI cleanup: ' + type(cleanup_error).__name__)
            if cancelled_by_close:
                raise ExecutionConflict('cancel_requested', 'CLI startup was cancelled') from error
            if self.resource_error is not None:
                code = 'cli_session_' + self.client.transport.resource_limit_reason
                raise ExecutionConflict(code, 'CLI startup exceeded its session resource limit') from error
            raise
        finally:
            self.starting = False
            self.start_finished.set()

    async def observe_storage(self):
        from .node_storage_budget import observe
        from .cli_runtime_storage import observe as observe_user
        try:
            while not self.stopping:
                # Keep potentially slow filesystem scans outside SQL locks and
                # the lease heartbeat. Drain the scan before retiring its task.
                await finish_before_cancel(asyncio.to_thread(observe, private=True))
                # Closure may start while the node inventory is in flight.
                # Drain that inventory, but do not start another user scan
                # which retirement would then have to cancel and drain too.
                if self.stopping:
                    return
                # A background task inherits the startup context when it is
                # created. Each runtime scan needs its own deadline after the
                # startup request has finished.
                async with request_deadline.scope(get_settings().storage_measurement_timeout_seconds, inherit=False):
                    await observe_user(self.user_id)
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self.stopping:
                logging.getLogger(__name__).warning('CLI storage check failed: session=%s observation=%s', self.id,
                    getattr(error, 'storage_observation', {'cause': type(error).__name__, 'code': getattr(error, 'code', None),
                        'underlying': type(error.__cause__).__name__ if error.__cause__ else None}))
                self.schedule_close(getattr(error, 'code', None) or type(error).__name__)

    async def renew(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self.scope_deadline is not None and time.monotonic()>=self.scope_deadline:
                    self.schedule_close('SessionLifetimeExceeded')
                    return
                if self.turn_execution is None and time.monotonic() - self.last_activity > IDLE_SECONDS:
                    self.schedule_close()
                    return
                async with SessionLocal() as db:
                    await authorize(db, self.case_id, self.conversation_id, self.user_id, expected=self.selected)
                    await records.heartbeat(db, self.owner)
                    await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.schedule_close(type(error).__name__)

    async def watch(self):
        try:
            await asyncio.shield(self.client._reader_task)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        self.schedule_close(self.resource_error or 'NativeStreamEnded')

    def schedule_close(self, error=None):
        operation = asyncio.create_task(self.close(error=error))
        operation.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def close(self, *, error=None):
        execution = self.turn_execution
        if execution is not None and asyncio.current_task() is not execution.task and not execution.finalizing:
            await execution.stop(error=error)
            return self.client.stop_result
        if self.close_task is None:
            self.stopping = True
            self.close_error = error
            self.close_task = asyncio.create_task(self.close_work(error))
        return await finish_before_cancel(self.close_task)

    async def close_work(self, error):
        if self.starting and self.start_task is not None and self.start_task is not asyncio.current_task() and not self.start_task.done():
            self.start_task.cancel()
            # The startup caller may still need to await close() to finish its own job.
            await self.start_finished.wait()
        return await self.retire(error)

    async def retire(self, error=None):
        if self.retire_task is None:
            self.stopping = True
            self.retire_task = asyncio.create_task(self.retire_work(error))
        return await finish_before_cancel(self.retire_task)

    async def retire_work(self, error):
        stop = None
        management = None
        failure = error
        try:
            if self.client is not None:
                try:
                    stop = await self.client.close()
                except Exception as cleanup_error:
                    stop = self.client.stop_result
                    failure = failure or type(cleanup_error).__name__
            failure = self.resource_error or failure
            self.cleanup_verified = stop.get('verified') if stop else None
            transport = self.client.transport if self.client is not None else None
            not_started = (transport is None or
                           (not transport.registered if self.runtime == 'docker' else transport.owned.proc is None))
            if not_started:
                self.cleanup_verified = True
            if self.scope_owner is not None:
                try:
                    from ..models import CliScopeAllocation
                    # A failed SQL commit can leave in-memory owner objects.
                    # Decide from committed rows before choosing the authority.
                    async with SessionLocal() as db:
                        allocation=await db.get(CliScopeAllocation,self.id)
                        committed=await db.get(NativeCliSession,self.id)
                    if committed is None:
                        self.owner=None
                        if allocation is not None:
                            from .cli_scope_allocation import retire
                            await retire(self.scope_owner)
                    else:
                        from .cli_scope_lifecycle import close as close_scope
                        management=await close_scope(self)
                        self.cleanup_verified=True
                        if not not_started and (stop is None or not stop.get('verified')):
                            failure=failure or 'NativeStopReceiptUnavailable'
                except Exception as cleanup_error:
                    self.cleanup_verified=False
                    failure=failure or type(cleanup_error).__name__
            if self.stage is not None:
                from ..routers.terminal import _cleanup_staging
                try:
                    if self.client is None or not self.client.transport.started:
                        await finish_before_cancel(asyncio.to_thread(_cleanup_staging, self.stage))
                    elif self.cleanup_verified and self.selected['permission_mode'] in READ_ONLY:
                        await finish_before_cancel(asyncio.to_thread(_cleanup_staging, self.stage))
                    elif self.cleanup_verified and failure is None:
                        async def authorized(db):
                            case, _ = await authorize(db, self.case_id, self.conversation_id, self.user_id, expected=self.selected)
                            if self.startup_guard is not None:
                                await self.startup_guard(db)
                            await records.current(db,self.owner)
                            if str(storage._fs_path(self.stage,'.').resolve())!=self.selected['cwd']:
                                raise CaseWriteConflict('input_stale','CLI 工作目录已变化')
                            return case
                        from .staging_sync import sync_authorized
                        await sync_authorized(self.stage,self.selected['source_prefix'],self.case_id,authorized)
                        await finish_before_cancel(asyncio.to_thread(_cleanup_staging, self.stage, synchronized=True))
                except Exception as stage_error:
                    failure = failure or type(stage_error).__name__
            if self.docker_stage is not None and self.cleanup_verified:
                try:
                    await self.finish_docker_stage(publish=failure is None)
                except CaseWriteConflict:
                    failure = failure or 'DockerCasePublicationConflict'
                except Exception as stage_error:
                    failure = failure or type(stage_error).__name__
            if self.owner is not None:
                root = (self.client.transport.receipt if not not_started else None) if self.runtime == 'docker' else self.client.transport.owned.record
                await records.complete(self.owner, root, stop if root is not None else None,
                    error=failure or (None if self.cleanup_verified else 'ProcessCleanupUnverified'),
                    exit_code=self.client.transport.exit_code, not_started=not_started,management=management)
                self.persisted = True
                if self.runtime=='wsl':
                    from .cli_initialization_capacity import closed
                    await closed(self.user_id)
            self.error = failure
            if failure:
                raise RuntimeError('CLI session failed: ' + failure)
            return stop
        finally:
            for task in (self.watch_task, self.heartbeat_task, self.storage_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (self.watch_task, self.heartbeat_task, self.storage_task) if task is not None), return_exceptions=True)
            if self.persisted and self.cleanup_verified and not self.pending_stage():
                SESSIONS.pop(self.id, None)
                unregister('codex:' + self.id)
            if self.cleanup_verified:
                self.secret = None
                if self.client is not None:
                    self.client.env.pop(credentials.TOKEN_ENV, None)
                    self.client.transport.env.pop(credentials.TOKEN_ENV, None)

    def pending_stage(self):
        return self.stage is not None and any(path.exists() for path in (
            storage._fs_path(self.stage, '.'), storage._staging_manifest_path(self.stage), storage._terminal_cleanup_marker(self.stage)))

    async def rpc(self, method, *, cursor=None, limit=50):
        if self.turn_execution is not None:
            raise ExecutionConflict('state_conflict', 'CLI is executing a native turn')
        async with self.operation_lock:
            if self.stopping:
                raise ExecutionConflict('state_conflict', 'CLI session is closing')
            try:
                async with self.access() as db:
                    await records.check(db, self.owner, self.client.transport)
                self.last_activity = time.monotonic()
                if method == 'account':
                    value = await self.client.get_account()
                    if not isinstance(value, dict) or (value.get('account') is not None and not isinstance(value['account'], dict)):
                        raise ValueError('Invalid CLI account response')
                elif method == 'models':
                    value = await self.client.list_models(cursor=cursor, limit=limit)
                    if (not isinstance(value, dict) or not isinstance(value.get('data'), list) or len(value['data']) > 100
                            or any(not isinstance(item, dict) for item in value['data'])
                            or (value.get('nextCursor') is not None and (not isinstance(value['nextCursor'], str)
                                or len(value['nextCursor']) > 4096 or '\0' in value['nextCursor']))):
                        raise ValueError('Invalid CLI model response')
                elif method == 'sandbox':
                    from .codex_sandbox import readiness_value
                    value = ({'status': 'not_applicable'} if self.runtime != 'windows' else
                             readiness_value(await self.client.windows_sandbox_readiness()))
                else:
                    raise ValueError('Unsupported CLI display request')
                async with self.access() as db:
                    await records.check(db, self.owner, self.client.transport)
                return self.clean(value)
            except CodexRpcError:
                raise
            except BaseException as error:
                try:
                    await finish_before_cancel(self.close(error='DisplayRequestFailed'))
                except Exception as cleanup_error:
                    error.add_note('CLI cleanup: ' + type(cleanup_error).__name__)
                raise

    async def create_thread(self, *, resume=False, goal_source_turn_id=None):
        if self.turn_execution is not None and asyncio.current_task() is not self.turn_execution.task:
            raise ExecutionConflict('state_conflict', 'CLI thread is reserved by a native turn')
        async with self.operation_lock:
            if self.stopping:
                raise ExecutionConflict('state_conflict', 'CLI session is closing')
            admitted = False
            try:
                await asyncio.to_thread(self.verify_code)
                async with self.access() as db:
                    await records.thread_start(db, self.owner, self.client.transport)
                    admitted = True
                permission = self.selected['permission_mode']
                approval, sandbox = THREAD_POLICIES[permission]
                if resume:
                    expected_turn = None
                    from ..models import NativeCliTurn, NativeCliFork
                    from sqlalchemy import select
                    async with SessionLocal() as db:
                        previous = await db.scalar(select(NativeCliTurn).where(NativeCliTurn.conversation_id == self.conversation_id,
                            NativeCliTurn.created_by == self.user_id, NativeCliTurn.phase == 'completed')
                            .order_by(NativeCliTurn.created_at.desc(), NativeCliTurn.id.desc()).limit(1))
                        if goal_source_turn_id is not None and (previous is None or previous.id != goal_source_turn_id):
                            raise ExecutionConflict('input_stale', '原生目标回合已变化')
                        if previous is None:
                            from .codex_forks import public as fork_public
                            previous = await db.scalar(select(NativeCliFork).where(NativeCliFork.conversation_id == self.conversation_id,
                                NativeCliFork.created_by == self.user_id))
                            if previous is None or (await fork_public(db, previous))['phase'] != 'completed':
                                raise ExecutionConflict('state_conflict', 'No verified native thread is available to resume')
                        else:
                            expected_turn = previous.native_turn_id
                        prior = await db.get(NativeCliSession, previous.session_id)
                        old = json.loads(prior.profile_json)
                        approved_plan = old['permission_mode'] == 'plan' and permission == 'approve_writes'
                        if records.public(prior)['cleanup_verified'] is not True or any(old[key] != self.selected[key] for key in (
                                'runtime', 'program_sha256', 'auth_config_sha256', 'auth_home', 'source_prefix')) or old.get('model', '') != self.selected.get('model', '') or old.get('credential') != self.credential or old.get('distro') != self.distro or old.get('docker_runtime') != self.selected.get('docker_runtime') or (old['permission_mode'] != permission and not approved_plan):
                            raise ExecutionConflict('input_stale', 'Native thread environment changed; use a new thread')
                        identifier = previous.thread_id
                    result = await self.client.resume_thread(identifier, exclude_turns=True, cwd=self.client.transport.profile['cwd'],
                        approval_policy=approval, sandbox=sandbox,
                        model=self.selected.get('model') or (self.credential or {}).get('default_model') or None)
                    if result.get('thread', {}).get('id') != identifier:
                        raise ValueError('CLI resumed another native thread')
                    if expected_turn is not None:
                        latest = await self.client.latest_turn(identifier)
                        if latest.get('id') != expected_turn or latest.get('status') != 'completed':
                            raise ExecutionConflict('input_stale', '原生线程最后回合已变化，请使用文字交接')
                    self.resumed_approved_plan = approved_plan
                else:
                    from .codex_case_tools import READ_INSTRUCTIONS, READ_TOOLS

                    dynamic_tools = READ_TOOLS if self.case_id is not None and permission in READ_ONLY else None
                    result = await self.client.new_thread(cwd=self.client.transport.profile['cwd'], sandbox=sandbox, approval_policy=approval,
                        model=self.selected.get('model') or (self.credential or {}).get('default_model') or None,
                        dynamic_tools=dynamic_tools, developer_instructions=READ_INSTRUCTIONS if dynamic_tools else None)
                self.thread_model = result.get('model')
                thread_id = result['thread']['id']
                self.check_identifier(thread_id)
                self.check_identifier(self.thread_model)
                async with self.access() as db:
                    await records.thread_ready(db, self.owner, self.client.transport, thread_id)
                self.last_activity = time.monotonic()
                return {'thread_id': thread_id}
            except BaseException as error:
                if admitted or not (isinstance(error, ExecutionConflict) and error.code == 'idempotency_conflict'):
                    # The native actor owns retirement in its finally block.
                    # Wrapping close() in a separate task here would make it
                    # join that same actor, which is waiting for this cleanup.
                    if self.turn_execution is None or asyncio.current_task() is not self.turn_execution.task:
                        try:
                            await finish_before_cancel(self.close(error='ThreadCreationUnresolved'))
                        except Exception as cleanup_error:
                            error.add_note('CLI cleanup: ' + type(cleanup_error).__name__)
                raise


async def create(case_id, conversation_id, user_id, *, provider_profile_id=None, provider_wire_api=None, runtime='windows'):
    return await Session(case_id, conversation_id, user_id, provider_profile_id=provider_profile_id,
                         provider_wire_api=provider_wire_api, runtime=runtime).start()


async def row(identifier, user_id):
    async with SessionLocal() as db:
        value = await db.get(NativeCliSession, identifier)
        if value is None or value.created_by != user_id:
            raise HTTPException(404, 'CLI 会话不存在')
        user = await db.get(User, user_id, populate_existing=True)
        if user is None or user.status != 'active':
            raise HTTPException(403, 'CLI 用户已不可用')
        return value


async def live(identifier, user_id):
    value = await row(identifier, user_id)
    session = SESSIONS.get(identifier)
    if session is None or session.user_id != user_id or session.owner is None or records.public(value)['phase'] != 'active':
        raise ExecutionConflict('recovery_required', 'CLI owner is unavailable; history is read-only')
    if session.fork_only:
        raise ExecutionConflict('state_conflict', '原生分叉使用专用会话，请在分支中另行发送消息')
    return session


async def close_all():
    results = await asyncio.gather(*(session.close() for session in list(SESSIONS.values())), return_exceptions=True)
    if any(isinstance(result, BaseException) for result in results):
        raise RuntimeError('One or more CLI sessions could not complete closure')
