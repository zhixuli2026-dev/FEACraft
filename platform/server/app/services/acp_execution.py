"""Platform-owned OpenCode/Kimi turns using the ACP transport."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timezone
from functools import partial
import hashlib
import json
import logging
import os
from pathlib import Path
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import AcpThread, AcpTurn, AgentEvent, Conversation, ProviderProfile, User
from . import acp_approval, acp_case_files, acp_runtime, audit, cli_contracts, conversation_execution as control
from . import docker_acp, docker_cli_usage
from . import process_identity_native, process_record, process_stop
from .acp_session import AcpSession
from .async_ops import finish_before_cancel
from .case_write import bounded_case_read, coordinate
from .cli_private_paths import root as private_root
from .conversation_history import event_text_field
from .codex_projection import Projection
from .durable_events import json_text, now
from .owned_stdio import OwnedStdio
from .quota import admit_model
from .rate_limit import provider as admit_provider
from .supervisor import register, unregister
from .terminal_credentials import descriptor, select as select_credential
from .workflow_state import ExecutionConflict


ACTIVE = {}
logger = logging.getLogger(__name__)
TERMINAL = {'completed', 'failed', 'cancelled', 'recovery_required'}
MAX_REPLY = 8 * 1024 * 1024
STATE_ALLOWANCE = 16 * 1024 * 1024


async def close_all():
    running = list(ACTIVE.values())
    for turn in running:
        turn.cancelled.set()
    await asyncio.gather(*(turn.stop() for turn in running))


async def owned_case(db, cid, user, *, write=True):
    from ..routers.workspaces import get_owned_case
    conversation = await db.get(Conversation, cid, populate_existing=True)
    if conversation is None:
        raise HTTPException(404, '会话不存在')
    return await get_owned_case(conversation.case_id, user, db, write=write)


def cleanup_evidence(turn):
    if turn.process_json is None or turn.stop_json is None:
        return None
    try:
        raw = json.loads(turn.process_json)
        stop = json.loads(turn.stop_json)
        if isinstance(raw, dict) and raw.get('schema') == docker_acp.SCHEMA:
            return stop if docker_acp.verified(raw, stop, turn) else None
        receipt = process_record.validate(raw, bound=False)
        cleanup = process_stop.validate(stop, receipt)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return cleanup if cleanup['verified'] and cleanup['scope_empty'] else None


def verified_cleanup(turn):
    return cleanup_evidence(turn) is not None


def public(turn, thread):
    cleanup = cleanup_evidence(turn)
    result = {'id': turn.id, 'thread_id': thread.id, 'harness': thread.harness, 'runtime': thread.runtime,
            'execution_backend': 'docker' if thread.host_id.startswith('docker:') else 'host',
            'provider_profile_id': thread.profile_id,
            'phase': turn.phase, 'client_message_id': turn.client_message_id, 'error': turn.error,
            'cleanup_verified': cleanup is not None,
            'created_at': turn.created_at.replace(tzinfo=timezone.utc).isoformat(),
            'contract_applied':thread.contract_text is not None, 'allow_drafts':thread.allow_drafts}
    if cleanup and cleanup.get('schema') == docker_acp.SCHEMA and 'resource_usage' in cleanup:
        usage = cleanup['resource_usage']
        try:
            docker_cli_usage.validate(usage, cleanup['containers']['worker']['id'])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            result['resource_usage'] = usage
    return result


async def lookup(db, identifier, user):
    turn = await db.get(AcpTurn, identifier, populate_existing=True)
    if turn is None or turn.created_by != user.id:
        raise HTTPException(404, '原生回合不存在')
    await owned_case(db, turn.conversation_id, user, write=False)
    return turn, await db.get(AcpThread, turn.thread_id, populate_existing=True)


async def submit(db, cid, user, harness, *, profile_id, client_id, content, thread_id, allow_drafts, case_files=()):
    from ..routers.agent import _append_event

    if harness not in acp_runtime.HARNESSES:
        raise HTTPException(404, '不支持的原生工具')
    case = await owned_case(db, cid, user)
    try:
        paths = acp_case_files.selection(case_files)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    request_parts = [harness, profile_id, thread_id, content, allow_drafts]
    if paths:
        request_parts.append(paths)
    request_digest = hashlib.sha256(json_text(request_parts).encode()).hexdigest()
    key = select(AcpTurn).where(AcpTurn.conversation_id == cid, AcpTurn.created_by == user.id,
                               AcpTurn.client_message_id == client_id)

    async def replay(existing):
        if existing.request_sha256 != request_digest:
            raise HTTPException(409, '消息标识已用于其他提交')
        return public(existing, await db.get(AcpThread, existing.thread_id))

    existing = await db.scalar(key)
    if existing is not None:
        return await replay(existing)
    settings = get_settings()
    prefix = case.s3_prefix
    inputs = {}
    if paths:
        from .agent_storage_capacity import read_case
        from .case_write import CaseWriteConflict
        from .file_read import FileReadConflict
        if not settings.acp_docker_image:
            raise HTTPException(422, '算例文件选择需要 Docker ACP 环境')
        try:
            inputs = await read_case(case.id, prefix, user.id, acp_case_files.capture, prefix, paths,
                                     code='acp_case_inputs', label='Agent 算例文件读取')
        except (CaseWriteConflict, FileReadConflict) as error:
            raise HTTPException(409, '算例文件正在变化，请重新选择后提交') from error
    try:
        docker_runtime = await docker_acp.configured(settings)
    except (OSError, RuntimeError, ValueError) as error:
        logger.warning('Docker ACP runtime unavailable: %s', error)
        raise HTTPException(503, 'Docker ACP 执行环境当前不可用') from error
    contract_text, selected_versions = None, None
    if thread_id is None:
        from .case_read_capacity import file_read_deadline
        from .file_read import FileReadConflict
        async with file_read_deadline('cli_contract', 'CLI 约定'):
            try:
                state, files, _, _, selected_versions = await bounded_case_read(
                    cli_contracts.capture, prefix, code='cli_contract', label='CLI 约定')
                text = files['AGENTS.md'] or ''
                if state.selected == harness and cli_contracts.remove_managed(text) != text:
                    contract_text = text
                    if profile_id is None:
                        profile_id = cli_contracts.profile_ref(state, user.id)
            except FileReadConflict as error:
                raise HTTPException(409, 'CLI 约定正在变化，请重试') from error
            except (ValueError, UnicodeError) as error:
                raise HTTPException(422, 'CLI 约定无法读取，请检查说明文件与备份') from error
    async with coordinate(db, case):
        await db.refresh(user)
        if user.status != 'active':
            raise HTTPException(401, '用户不可用')
        await owned_case(db, cid, user)
        existing = await db.scalar(key)
        if existing is not None:
            return await replay(existing)
        if inputs and case.s3_prefix != prefix:
            raise HTTPException(409, '算例来源已变化，请重新选择后提交')
        if selected_versions is not None:
            if case.s3_prefix != prefix:
                raise HTTPException(409, '算例来源已变化，请重试')
            try:
                await bounded_case_read(cli_contracts.verify_versions, prefix, selected_versions,
                                        code='cli_contract', label='CLI 约定')
            except FileReadConflict as error:
                raise HTTPException(409, 'CLI 约定正在变化，请重试') from error
        thread = await db.get(AcpThread, thread_id, populate_existing=True) if thread_id else None
        if thread_id is not None:
            if thread is None or (thread.conversation_id, thread.created_by) != (cid, user.id):
                raise HTTPException(404, '原生会话不存在')
            if profile_id is None:
                profile_id = thread.profile_id
        if profile_id is None:
            raise HTTPException(422, '请选择本人 API 配置，或为当前工具保存算例默认配置')
        profile = await db.get(ProviderProfile, profile_id, populate_existing=True)
        if profile is None or profile.org_id is not None or profile.user_id != user.id:
            raise HTTPException(404, '原生配置不存在')
        credential = descriptor(profile, harness)
        if docker_runtime and (credential['provider'] != 'deepseek' or credential['wire_api'] != 'openai'
                or credential['base_url'].rstrip('/') not in {'https://api.deepseek.com', 'https://api.deepseek.com/v1'}):
            raise HTTPException(422, 'Docker ACP 需要本人配置的官方 DeepSeek API')
        host = docker_runtime.host_id if docker_runtime else process_identity_native.host_id()
        runtime = 'linux' if docker_runtime or os.name != 'nt' else 'windows'
        if thread is not None:
            if ((thread.harness, thread.runtime, thread.profile_id, thread.profile_sha256, thread.host_id) !=
                    (harness, runtime, profile_id, credential['revision'], host)):
                raise HTTPException(409, '原生会话的配置或执行环境已变化，请新建会话')
            if allow_drafts is not None and allow_drafts != thread.allow_drafts:
                raise HTTPException(409, '权限模式已变化，请新建原生线程')
        _, secret = await select_credential(db, profile_id, user.id, harness, 'cmd')
        try:
            argv = None if docker_runtime else acp_runtime.command(harness)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(503, '原生工具当前无法使用') from error
        identifier = str(uuid.uuid4())
        owner = await control.acquire(db, cid, 'api', identifier)
        from .quota import check_storage_quota
        await check_storage_quota(db, user, STATE_ALLOWANCE)
        from .linux_account_capacity import admit
        await admit(db, user.id, docker=docker_runtime is not None)
        await admit_model(db, user.id, harness)
        await admit_provider(user.id, profile_id)
        if thread is None:
            thread = AcpThread(id=str(uuid.uuid4()), conversation_id=cid, created_by=user.id, harness=harness,
                runtime=runtime, profile_id=profile_id, profile_sha256=credential['revision'], host_id=host,
                contract_text=contract_text, allow_drafts=bool(allow_drafts), created_at=now())
            db.add(thread)
            await db.flush()
        turn = AcpTurn(id=identifier, thread_id=thread.id, conversation_id=cid, created_by=user.id,
                       client_message_id=client_id, request_sha256=request_digest, phase='admitted', created_at=now())
        db.add(turn)
        await _append_event(db, cid, 'message', {'role':'user', 'content':content, 'backend':harness,
            'acp_turn_id':identifier, 'client_user_message_id':client_id, 'quota_origin':'daily'})
        await _append_event(db, cid, 'run_state', {'run_id':identifier, 'status':'started', 'backend':harness,
            'execution_binding':hashlib.sha256(owner.token.encode()).hexdigest()})
        if inputs:
            await _append_event(db, cid, 'acp', {'turn_id':identifier, 'type':'case_inputs',
                                'files':acp_case_files.receipt(inputs)})
        await audit.case_event(db, case, user.id, 'acp.turn.admitted', 'acp_turn', identifier,
                               {'harness':harness, 'provider_profile_id':profile_id,
                                'execution_backend':'docker' if docker_runtime else 'host'})
    async def launch_after_commit():
        await db.commit()
        execution = Execution(owner, turn, thread, case.id, argv, credential, secret, content,
                              docker_runtime=docker_runtime, inputs=inputs, source_prefix=prefix)
        ACTIVE[identifier] = execution
        register('acp', identifier, case.id, execution.stop)
        execution.task = asyncio.create_task(execution.run())
        execution.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    await finish_before_cancel(launch_after_commit())
    return public(turn, thread)


async def prompt_text(db, turn):
    statement = select(AgentEvent.payload).where(AgentEvent.conversation_id == turn.conversation_id,
        AgentEvent.type == 'message', event_text_field(db, 'role') == 'user',
        event_text_field(db, 'acp_turn_id') == turn.id)
    raw = await db.scalar(statement.limit(1))
    return json.loads(raw)['content'] if raw else ''


class Execution:
    def __init__(self, owner, turn, thread, case_id, argv, credential, secret, content, *,
                 docker_runtime=None, inputs=None, source_prefix=None):
        self.owner, self.id = owner, turn.id
        self.thread, self.case_id, self.argv = thread, case_id, argv
        self.credential, self.secret, self.content = credential, secret, content
        self.docker_runtime = docker_runtime
        self.inputs = inputs or {}
        self.source_prefix = source_prefix
        self.session = self.projection = self.task = None
        self.cancelled = asyncio.Event()
        self.answer = []
        self.answer_bytes = 0
        self.approved_files = {}
        self.native_inputs = {}

    async def stop(self):
        self.cancelled.set()
        if self.task is not None and self.task is not asyncio.current_task():
            await finish_before_cancel(asyncio.gather(self.task, return_exceptions=True))

    async def record(self, value):
        from ..routers.agent import _append_event
        from .durable_events import lock_case_execution
        async with SessionLocal.begin() as db:
            await lock_case_execution(db, self.case_id)
            await control.current(db, self.owner, launch=False)
            await _append_event(db, self.thread.conversation_id, 'acp', {'turn_id':self.id, **value})

    async def register_root(self, receipt):
        async with SessionLocal.begin() as db:
            await control.current(db, self.owner)
            turn = await db.get(AcpTurn, self.id)
            if turn is None or turn.phase != 'admitted' or turn.process_json is not None:
                raise ExecutionConflict('state_conflict', '原生回合启动状态已变化')
            if self.docker_runtime:
                docker_acp.proof(receipt, turn)
            else:
                process_record.validate(receipt, bound=False)
            turn.process_json = json_text(receipt)

    async def reauthorize(self, _):
        if self.cancelled.is_set():
            raise asyncio.CancelledError
        async with SessionLocal.begin() as db:
            await control.current(db, self.owner)
            user = await db.get(User, self.thread.created_by, populate_existing=True)
            if user is None or user.status != 'active':
                raise HTTPException(401, '用户不可用')
            case = await owned_case(db, self.thread.conversation_id, user)
            if self.inputs and case.s3_prefix != self.source_prefix:
                raise HTTPException(409, '算例来源已变化，请重新选择后提交')
            profile = await db.get(ProviderProfile, self.thread.profile_id, populate_existing=True)
            if (profile is None or profile.org_id is not None or profile.user_id != user.id
                    or descriptor(profile, self.thread.harness) != self.credential):
                raise HTTPException(409, '模型配置已变化')

    async def update(self, payload):
        kind = payload['sessionUpdate']
        content = payload.get('content')
        if kind in {'agent_message_chunk','agent_thought_chunk'} and isinstance(content, dict) and content.get('type') == 'text':
            text = content.get('text')
            if not isinstance(text, str):
                raise ValueError('Invalid ACP output')
            size = len(text.encode('utf8'))
            self.answer_bytes += size
            if self.answer_bytes > MAX_REPLY:
                raise ValueError('Native reply exceeds its limit')
            clean = self.projection.delta((kind, self.id), text)
            if clean:
                if kind == 'agent_message_chunk':
                    self.answer.append(clean)
                await self.record({'type':kind, 'text':clean})
        elif kind in {'tool_call','tool_call_update'}:
            if self.thread.allow_drafts:
                tool_id, arguments = payload.get('toolCallId'), payload.get('rawInput')
                if (isinstance(tool_id, str) and len(tool_id) <= 200 and isinstance(arguments, dict)
                        and len(self.native_inputs) < 32):
                    self.native_inputs[tool_id] = {'kind':payload.get('kind'), 'rawInput':arguments}
            selected = {k:payload[k] for k in ('toolCallId','title','kind','status') if k in payload}
            await self.record({'type':kind, **self.projection.secrets.clean(selected)})

    async def run(self):
        from ..routers.agent import _append_event
        from .durable_events import lock_case_execution

        heartbeat = asyncio.create_task(control.watch(self.owner, self.cancelled))
        prompt_task = cancel_task = None
        phase, error, stop = 'failed', None, None
        try:
            async with asyncio.timeout(240):
                platform = 'docker' if self.docker_runtime else self.thread.runtime
                home = private_root(get_settings())/'users'/str(self.thread.created_by)/platform/'acp'/self.thread.id
                cwd = home/'workspace'
                if home.resolve() != home or cwd.resolve() != cwd:
                    raise ValueError('Private harness state was redirected')
                cwd.mkdir(parents=True, exist_ok=True)
                contract_file = None
                if self.thread.contract_text is not None:
                    from .file_transactions import _path, _write_new
                    contract_file = _path(cwd, 'AGENTS.md')
                    expected = self.thread.contract_text.encode('utf8')
                    if contract_file.exists():
                        if contract_file.stat().st_size != len(expected) or contract_file.read_bytes() != expected:
                            raise ValueError('Native contract differs from its recorded snapshot')
                    else:
                        _write_new(contract_file, expected)
                if self.docker_runtime:
                    stage = acp_case_files.directory(get_settings(), self.thread.created_by, self.thread.id, self.id)
                    transport = docker_acp.Transport(self.docker_runtime, self.thread, self.id, cwd,
                        self.credential, self.secret, input_dir=stage, inputs=self.inputs)
                    self.projection = Projection(home, ephemeral=(self.secret, transport.capability))
                    native_cwd = '/workspace'
                else:
                    if any(Path(entry).is_absolute() and Path(entry).resolve().is_relative_to(Path(get_settings().storage_fs_root).resolve())
                           for entry in self.argv[:-1]):
                        raise ValueError('ACP executable is in publicly writable storage')
                    environment = acp_runtime.environment(self.thread.harness, home, endpoint=self.credential['base_url'],
                        model=self.credential['model'], api_key=self.secret, wire=self.credential['wire_api'],
                        managed_text_only=True, contract_file=contract_file, allow_file_approval=self.thread.allow_drafts)
                    self.projection = Projection(home, ephemeral=(self.secret,))
                    transport = OwnedStdio(self.argv, cwd=cwd, env=environment,
                                           lifetime_seconds=240, max_output_bytes=32*1024*1024)
                    native_cwd = str(cwd)
                self.secret = None
                permission = partial(acp_approval.request, self, cwd) if self.thread.allow_drafts else None
                self.session = AcpSession(transport, emit=self.update, request_permission=permission)
                if self.cancelled.is_set():
                    raise asyncio.CancelledError
                await self.session.start(native_cwd, session_id=self.thread.native_id,
                    before_release=self.register_root, before_start=self.reauthorize)
                async with SessionLocal.begin() as db:
                    await control.current(db, self.owner)
                    thread = await db.get(AcpThread, self.thread.id)
                    turn = await db.get(AcpTurn, self.id)
                    thread.native_id, turn.phase = self.session.session_id, 'running'
                content = self.content
                if self.inputs:
                    files = json.dumps(list(self.inputs), ensure_ascii=False)
                    content += f'\n\nFiles selected for this turn are mounted read-only at {acp_case_files.MOUNT}: {files}'
                prompt_task = asyncio.create_task(self.session.prompt(content))
                cancel_task = asyncio.create_task(self.cancelled.wait())
                await asyncio.wait((prompt_task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
                if self.cancelled.is_set():
                    phase = 'cancelled'
                    await self.session.cancel()
                    with suppress(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(prompt_task), 5)
                else:
                    result = await prompt_task
                    reason = result.get('stopReason')
                    if isinstance(reason, str) and reason in {'end_turn','refusal','cancelled','max_tokens','max_turn_requests'}:
                        await self.record({'type':'prompt_finished', 'reason':reason})
                    if reason in ('end_turn','refusal'):
                        phase = 'completed'
                    elif reason == 'cancelled':
                        phase = 'cancelled'
                    else:
                        error = 'PromptLimit' if reason in ('max_tokens','max_turn_requests') else 'UnexpectedStop'
        except asyncio.CancelledError:
            phase = 'cancelled'
        except Exception as failure:
            error = type(failure).__name__
        finally:
            for task in (prompt_task, cancel_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (prompt_task, cancel_task) if task is not None), return_exceptions=True)
            try:
                if self.session is not None:
                    reason = {'completed':'root_exit', 'failed':'executor_error', 'cancelled':'cancelled'}[phase]
                    stop = await finish_before_cancel(self.session.close(reason))
                async with SessionLocal.begin() as db:
                    await lock_case_execution(db, self.case_id)
                    await control.current(db, self.owner, launch=False)
                    turn = await db.get(AcpTurn, self.id)
                    turn.stop_json = json_text(stop) if stop else None
                    if turn.process_json is not None and not verified_cleanup(turn):
                        raise RuntimeError('ACP process exit is unverified')
                    turn.phase, turn.error, turn.ended_at = phase, error, now()
                    for name, before in sorted(self.approved_files.items()):
                        try:
                            path = acp_approval.checked_file(cwd, name)
                            after = acp_approval.file_stamp(path)
                            if after is None or after == before or after[2] > 65536:
                                continue
                            text = self.projection.secrets.clean(path.read_text(encoding='utf8'))
                        except (OSError, ValueError):
                            continue
                        await _append_event(db, self.thread.conversation_id, 'acp',
                            {'turn_id':self.id, 'type':'draft', 'path':name, 'text':text})
                    if phase == 'completed':
                        tail = self.projection.delta(('agent_message_chunk', self.id), '', final=True)
                        if tail:
                            await _append_event(db, self.thread.conversation_id, 'acp',
                                {'turn_id':self.id, 'type':'agent_message_chunk', 'text':tail})
                        await _append_event(db, self.thread.conversation_id, 'message', {'role':'assistant',
                            'content':''.join(self.answer)+tail, 'backend':self.thread.harness, 'run_id':self.id,
                            'acp_turn_id':self.id})
                    await _append_event(db, self.thread.conversation_id, 'run_state', {'run_id':self.id, 'status':phase,
                        'backend':self.thread.harness})
                    await control.release(self.owner, db=db)
            except Exception as failure:
                async with SessionLocal.begin() as db:
                    turn = await db.get(AcpTurn, self.id)
                    if turn is not None and turn.phase not in TERMINAL:
                        turn.phase, turn.error, turn.ended_at = 'recovery_required', type(failure).__name__, now()
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                ACTIVE.pop(self.id, None)
                unregister('acp:'+self.id)
