"""workspace/case 重命名、移动与深拷贝；不删除用户数据。"""
from contextlib import AsyncExitStack
from functools import partial
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..models import Case, Conversation, Job, JobAttempt, ToolCall, User, Workspace
from . import case_provision
from .case_write import coordinate
from .workspace_write import coordinate_workspaces
from .case_source import SourceChanged, source_manifest, verify_local_versions, verify_remote_versions, verify_source
from .casedict import CASE_DICT_PATH, prepare_case_dict, _apply_dict_mirror
from .case_events import CASE_EVENTS_PATH
from .provenance import PROVENANCE_PATH
from .quota import check_storage_quota

COPY_EXCLUDED = ('.vibeflow/attempts/', '.vibeflow/attempt-provisions/', '.vibeflow/publications/', '.vibeflow/file-transactions/', '.vibeflow/live-store/', '.vibeflow/uploads/', '.vibeflow/job-logs/')


async def _copy_manifest(prefix):
    return await case_provision.io(partial(source_manifest, prefix, exclude=COPY_EXCLUDED))


class CaseAdminError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def validate_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 200 or any(char in value for char in "/\\\x00"):
        raise CaseAdminError("名称不能为空、超过 200 字或包含路径分隔符")
    return value


async def _no_running_jobs(db: AsyncSession, case_id: int):
    running = await db.scalar(select(Job.id).where(
        Job.case_id == case_id, Job.status.in_(["queued", "running"])).limit(1))
    if running is not None:
        raise CaseAdminError("算例仍有排队或运行中的作业，请取消后再移动或复制", 409)
    from .workflow_state import TERMINAL
    active_attempt = await db.scalar(select(JobAttempt.id).join(Job, Job.id == JobAttempt.job_id).where(
        Job.case_id == case_id, JobAttempt.phase.not_in(TERMINAL)).limit(1))
    if active_attempt is not None:
        raise CaseAdminError("算例作业尝试尚未收尾，请等待后再移动或复制", 409)
    from ..models import RemoteHpcRun
    from .remote_hpc import TERMINAL as REMOTE_TERMINAL
    pending_hpc = await db.scalar(select(RemoteHpcRun.id).where(
        RemoteHpcRun.case_id == case_id, RemoteHpcRun.state.not_in(REMOTE_TERMINAL)).limit(1))
    if pending_hpc is not None:
        raise CaseAdminError("算例的远程调度作业尚未收尾，请等待或取消后重试", 409)


async def _copy_source_idle(db: AsyncSession, case: Case):
    await _no_running_jobs(db, case.id)
    await _no_conversation_executions(db, [case.id])
    await _no_cli_sessions(db, [case.id])
    await _no_agent_requests(db,[case.id])
    await _no_terminal_sessions(db, [case.id])
    shell = await db.scalar(select(ToolCall.id).join(Conversation).where(
        Conversation.case_id == case.id, ToolCall.tool == "run_shell_command",
        ToolCall.status.in_(["queued", "running"])).limit(1))
    if shell is not None:
        raise CaseAdminError("算例有执行中的 Agent 命令，请等待后再复制", 409)
    # These registries cover this process only; the content checks below also
    # detect observed bypass writes. They are not a distributed native-file lock.
    from ..routers import terminal, viz
    if any(s.alive and getattr(s, "case_id", None) == case.id for s in terminal._sessions.values()):
        raise CaseAdminError("算例有活动终端，请关闭后再复制", 409)
    await _no_viz_sessions(db,[case.id])


async def _no_viz_sessions(db,case_ids):
    from ..models import NativeVizSession
    from .viz_records import public
    rows=(await db.scalars(select(NativeVizSession).where(NativeVizSession.case_id.in_(case_ids)))).all()
    for row in rows:
        state=public(row)
        if state['phase'] in {'starting','active','recovery_required','evidence_invalid'} or (state['process'] is not None and state['cleanup_verified'] is not True):
            raise CaseAdminError('可视化会话尚未核验收尾，请关闭或对账后再操作',409)


async def _no_terminal_sessions(db, case_ids):
    """Caller holds the case control lock also used by terminal reservation."""
    from ..models import NativeSession
    from .terminal_records import public
    from .sandbox_retirement import verified as verified_retirement
    rows = await db.scalars(select(NativeSession).where(NativeSession.case_id.in_(case_ids))
        .execution_options(populate_existing=True))
    for row in rows:
        state = public(row)
        reconciled = False
        if row.retirement_event_id is not None:
            try:
                witness = await verified_retirement(db, row)
                reconciled = witness is not None
                if reconciled:
                    from .sandbox_staging_cleanup import require_finished
                    await require_finished(db, row, witness)
                    from .sandbox_image_cleanup import require_finished as image_finished
                    await image_finished(db, row, witness)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise CaseAdminError('终端收尾凭证不可校验，请先完成对账', 409) from error
        if not reconciled and (state['phase'] not in {'closed', 'failed'} or state['cleanup_verified'] is not True):
            raise CaseAdminError('终端会话尚未核验收尾，请关闭或对账后再操作', 409)
        # A stopped native process may still have unsynchronized data or a
        # partially removed staging tree. Do not strand that recovery evidence.
        try:
            stage = json.loads(row.profile_json)['staging_prefix']
            if stage is not None:
                storage._check_stage_prefix(stage)
                remaining = any(path.exists() for path in (storage._fs_path(stage, '.'),
                    storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage)))
                if remaining:
                    raise CaseAdminError('终端会话仍有待处理暂存，请先完成收尾', 409)
        except CaseAdminError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CaseAdminError('终端暂存证据不可校验，请先完成对账', 409) from error


async def _no_agent_requests(db,case_ids):
    from ..models import NativeRequest
    from .native_request_records import public
    rows=(await db.scalars(select(NativeRequest).where(NativeRequest.case_id.in_(case_ids),NativeRequest.kind=='agent.shell'))).all()
    for row in rows:
        state=public(row)
        if state['phase'] in {'active','recovery_required','evidence_invalid'} or (state['children'] is not None and state['children']['children'] and state['cleanup_verified'] is not True):
            raise CaseAdminError('Agent 原生命令尚未核验收尾，请等待或对账后再操作',409)


async def _no_cli_sessions(db, case_ids):
    from ..models import NativeCliSession
    from .codex_records import public
    rows = (await db.scalars(select(NativeCliSession).where(NativeCliSession.case_id.in_(case_ids)))).all()
    for row in rows:
        value = public(row)
        if value['phase'] not in {'closed', 'failed'} or value['cleanup_verified'] is not True:
            raise CaseAdminError('CLI 会话尚未核验收尾，请关闭或对账后再操作', 409)
        selected = json.loads(row.profile_json)
        stage = selected['staging_prefix']
        if stage is not None and any(path.exists() for path in (
                storage._fs_path(stage, '.'), storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage))):
            raise CaseAdminError('CLI 会话仍有待处理暂存，请先完成收尾', 409)


async def _no_conversation_executions(db, case_ids):
    from ..models import ConversationExecution
    active = await db.scalar(select(ConversationExecution.conversation_id).join(Conversation).where(
        Conversation.case_id.in_(case_ids), ConversationExecution.phase != 'idle').limit(1))
    if active is not None:
        raise CaseAdminError('会话仍有执行或待对账记录，请先完成收尾', 409)


async def assert_no_case_activity(db: AsyncSession, case_ids: list[int]):
    """回收站移动前阻断所有已知写入者，避免进程继续写旧路径。"""
    if not case_ids:
        return
    await _no_conversation_executions(db, case_ids)
    await _no_cli_sessions(db, case_ids)
    await _no_agent_requests(db,case_ids)
    await _no_terminal_sessions(db, case_ids)
    active_job = await db.scalar(select(Job.id).where(
        Job.case_id.in_(case_ids), Job.status.in_(["queued", "running"])).limit(1))
    if active_job is not None:
        raise CaseAdminError("存在排队或运行中的作业，请先取消", 409)
    from .workflow_state import TERMINAL
    active_attempt = await db.scalar(select(JobAttempt.id).join(Job, Job.id == JobAttempt.job_id).where(
        Job.case_id.in_(case_ids), JobAttempt.phase.not_in(TERMINAL)).limit(1))
    if active_attempt is not None:
        raise CaseAdminError("作业尝试尚未收尾，请等待后再操作", 409)
    job_ids = (await db.scalars(select(Job.id).where(Job.case_id.in_(case_ids)))).all()
    from . import jobrunner
    if any((proc := jobrunner.get_proc(job_id)) is not None and proc.ended_at is None
           for job_id in job_ids):
        raise CaseAdminError("作业执行器尚未收尾，请等待进程与日志关闭", 409)
    conversation_ids = (await db.scalars(select(Conversation.id).where(
        Conversation.case_id.in_(case_ids)))).all()
    if conversation_ids:
        active_tool = await db.scalar(select(ToolCall.id).where(
            ToolCall.conversation_id.in_(conversation_ids),
            ToolCall.status.in_(["pending", "queued", "running"])).limit(1))
        if active_tool is not None:
            raise CaseAdminError("存在待审批或执行中的 Agent 工具，请先处理", 409)
        from ..routers import agent
        if any(cid in agent._RUN_CANCELLATIONS or cid in agent._GOAL_CANCELLATIONS
               for cid in conversation_ids):
            raise CaseAdminError("存在运行中的 Agent/Goal 回合，请先停止", 409)
    from ..routers import terminal, viz
    await _no_viz_sessions(db,case_ids)
    if any(session.alive and getattr(session, "case_id", None) in case_ids
           for session in terminal._sessions.values()):
        raise CaseAdminError("存在活动终端会话，请先关闭", 409)


async def _case_name_free(db: AsyncSession, workspace_id: int, name: str, exclude_id: int | None = None):
    query = select(Case).where(Case.workspace_id == workspace_id, Case.name == name)
    if exclude_id is not None:
        query = query.where(Case.id != exclude_id)
    if await db.scalar(query.limit(1)) is not None:
        raise CaseAdminError("目标工作区已有同名算例", 409)


async def rename_workspace(db: AsyncSession, workspace: Workspace, user: User, name: str):
    name = validate_name(name)
    async with coordinate_workspaces(db, [workspace.id]):
        await db.refresh(workspace)
        existing = await db.scalar(select(Workspace).where(
            Workspace.user_id == user.id, Workspace.name == name, Workspace.id != workspace.id).limit(1))
        if existing is not None:
            raise CaseAdminError("同名工作区已存在", 409)
        workspace.name = name
    await db.commit()
    return workspace


async def rename_case(db: AsyncSession, case: Case, name: str):
    name = validate_name(name)
    workspace_id = case.workspace_id
    async with coordinate_workspaces(db, [workspace_id]):
        async with coordinate(db, case):
            if case.workspace_id != workspace_id:
                raise CaseAdminError("算例归属已变化，请刷新后重试", 409)
            await _case_name_free(db, case.workspace_id, name, case.id)
            case.name = name
    await db.commit()
    return case


async def move_case(db: AsyncSession, case: Case, target: Workspace):
    workspace_id = case.workspace_id
    async with coordinate_workspaces(db, [workspace_id, target.id]):
        async with coordinate(db, case):
            if case.workspace_id != workspace_id:
                raise CaseAdminError("算例归属已变化，请刷新后重试", 409)
            await _no_running_jobs(db, case.id)
            await _case_name_free(db, target.id, case.name, case.id)
            case.workspace_id = target.id
    await db.commit()
    return case


async def _copy_case_binding(db, source):
    from ..models import CaseEventCounter

    sequence = await db.scalar(select(CaseEventCounter.next_seq).where(CaseEventCounter.case_id == source.id))
    return (source.workspace_id, source.s3_prefix, source.dict_revision, source.workflow_state,
            source.case_meta, source.parse_status, sequence)


async def _copy_selection(db, source, target, user_id):
    """Capture the public source while both workspaces and case are coordinated."""
    from .sharing import case_access_role

    await db.refresh(target)
    if target.user_id != user_id:
        raise CaseAdminError('工作区不存在', 404)
    workspace = await db.get(Workspace, source.workspace_id, populate_existing=True)
    if workspace is None or await case_access_role(db, workspace, source, user_id, write=True) is None:
        raise CaseAdminError('算例不存在', 404)
    return await _copy_case_binding(db, source)


async def _prepare_public_copy_source(db, source, target, user_id):
    from .async_ops import finish_before_cancel
    from .owner_storage_budget import hold as owner_capacity

    source_workspace_id = source.workspace_id
    async with owner_capacity(db, user_id):
        async with coordinate_workspaces(db, [source_workspace_id, target.id]):
            async with coordinate(db, source, content_write=False):
                if source.workspace_id != source_workspace_id:
                    raise SourceChanged('源算例归属已变化，请重试')
                selected = await _copy_selection(db, source, target, user_id)
                await _copy_source_idle(db, source)
                prefix = source.s3_prefix
        await finish_before_cancel(db.commit())
    return selected, await _copy_manifest(prefix)


async def _prepare_local_copies(stack, db, user, manifests):
    from . import case_copy_local, owned_staging
    from .async_ops import finish_before_cancel
    from .file_stage_locations import MOUNTED_SCHEMA

    if not manifests:
        return {}
    location = await case_provision.io(case_copy_local.staging_location, user.id, '.case-copy-staging')
    if location.local:
        await case_provision.io(owned_staging.recover_abandoned, location.scratch, MOUNTED_SCHEMA)
        await case_provision.recover_owner(db, user.id)
    total = sum(file.size for manifest in manifests.values() for file in manifest.values())
    await check_storage_quota(db, user, total)
    await finish_before_cancel(db.commit())
    return await stack.enter_async_context(case_copy_local.prepare(location, manifests, COPY_EXCLUDED))


async def _prepare_copies(stack, db, user, manifests, kind):
    if not storage._use_s3():
        return await _prepare_local_copies(stack, db, user, manifests)
    if not manifests:
        return {}
    from . import case_copy_remote
    from .async_ops import finish_before_cancel
    from .owner_storage_budget import hold as owner_capacity

    if any(CASE_DICT_PATH not in manifest for manifest in manifests.values()):
        raise CaseAdminError('源算例缺少 caseDict，无法复制', 409)
    await finish_before_cancel(db.commit())
    await stack.enter_async_context(owner_capacity(db, user.id))
    await case_provision.recover_owner(db, user.id)
    total = sum(file.size for manifest in manifests.values() for file in manifest.values())
    await check_storage_quota(db, user, total)
    await finish_before_cancel(db.commit())
    return {case_id: await case_provision.io(case_copy_remote.stage, user.id, case_id,
                                            manifest, kind, COPY_EXCLUDED)
            for case_id, manifest in manifests.items()}


async def _verify_copy_versions(prefix, manifest, *, remote):
    verify = verify_remote_versions if remote else verify_local_versions
    await case_provision.io(partial(verify, prefix, manifest, exclude=COPY_EXCLUDED))


async def copy_case(db: AsyncSession, source: Case, target: Workspace, user: User,
                    name: str, commit: bool = True, check_quota: bool = True, *, prepare_source: bool = False):
    from .owner_storage_budget import hold as owner_capacity

    user_id = user.id
    selection = None
    name = validate_name(name)
    async with AsyncExitStack() as preparation:
        try:
            prepared = {}
            if prepare_source and commit:
                selection, manifest = await _prepare_public_copy_source(db, source, target, user_id)
                await _case_name_free(db, target.id, name)
                try:
                    prepared = await _prepare_copies(preparation, db, user, {source.id: manifest}, 'case.copy')
                except SourceChanged as error:
                    raise SourceChanged('复制期间源算例已变化，请重试') from error
            remote = bool(prepared) and storage._use_s3()
            async with owner_capacity(db, user_id):
                if not remote:
                    await case_provision.recover_owner(db, user_id)
                async with AsyncExitStack() as locks:
                    workspaces = [target.id] if selection is None else [selection[0], target.id]
                    await locks.enter_async_context(coordinate_workspaces(db, workspaces))
                    await locks.enter_async_context(coordinate(db, source))
                    if selection is not None and await _copy_selection(db, source, target, user_id) != selection:
                        raise SourceChanged('复制期间源算例已变化，请重试')
                    await _copy_source_idle(db, source)
                    if selection is None:
                        manifest = await _copy_manifest(source.s3_prefix)
                    total_bytes = sum(file.size for file in manifest.values())
                    if check_quota and not prepared:
                        await check_storage_quota(db, user, total_bytes)
                    copied, prefix, _ = await _copy_case_locked(db, source, target, user, name,
                                                                 manifest, prepared=prepared.get(source.id))
                    if prepared:
                        await _verify_copy_versions(source.s3_prefix, manifest, remote=remote)
                if commit:
                    await db.commit()
        except BaseException as error:
            await db.rollback()
            if isinstance(error, SourceChanged):
                raise CaseAdminError(str(error), 409) from error
            raise
        return copied, prefix, total_bytes


async def _copy_case_locked(db, source, target, user, name, manifest, kind='case.copy', *, prepared=None):
    """No commits/lock acquisition: workspace copy locks all sources first."""
    await _case_name_free(db, target.id, name)
    copied = Case(
        workspace_id=target.id, name=name, s3_prefix="",
        workflow_state=source.workflow_state, case_meta=source.case_meta,
        parse_status=source.parse_status, dict_revision=source.dict_revision - 1,
    )
    db.add(copied)
    await db.flush()
    if CASE_DICT_PATH not in manifest:
        raise CaseAdminError("源算例缺少 caseDict，无法复制", 409)
    source_prefix = source.s3_prefix
    if prepared is None:
        selected = {path: await case_provision.io(manifest[path].read_content)
                    for path in (CASE_DICT_PATH, CASE_EVENTS_PATH, PROVENANCE_PATH) if path in manifest}
    else:
        selected = prepared.metadata
        await _verify_copy_versions(source_prefix, manifest, remote=storage._use_s3())
    rendered = prepare_case_dict(copied, selected[CASE_DICT_PATH].decode('utf8'), source='case-copy',
                                 existing_files=selected, actor_id=user.id)
    final = {path: file.record for path, file in manifest.items()}
    final.update({path: case_provision.digest(data) for path, data in rendered.files.items()})
    _apply_dict_mirror(copied, rendered)
    if prepared is None:
        await case_provision.create_from_manifest(db, copied, user.id, final,
            lambda path: rendered.files[path] if path in rendered.files else manifest[path],
            kind, lambda: verify_source(source_prefix, manifest, exclude=COPY_EXCLUDED))
    elif storage._use_s3():
        await prepared.publish(db, copied, final, rendered.files)
    else:
        additional = sum(record['size'] for record in final.values()) - prepared.accounted_bytes(user.id)
        await check_storage_quota(db, user, additional)
        await prepared.publish(db, copied, user.id, final, rendered.files, kind)
    return copied, copied.s3_prefix, sum(file.size for file in manifest.values())


async def _workspace_copy_members(db, locks, source, *, content_write):
    from .trash import is_case_deleted

    rows = await db.scalars(select(Case).where(Case.workspace_id == source.id)
                           .order_by(Case.id).execution_options(populate_existing=True))
    cases = [case for case in rows if not is_case_deleted(case.id)]
    # Match the original workspace-before-case lock order and preserve idle checks.
    for case in cases:
        await locks.enter_async_context(coordinate(db, case, content_write=content_write))
        if case.workspace_id != source.id or is_case_deleted(case.id):
            raise CaseAdminError('源工作区算例归属已变化，请刷新后重试', 409)
        await _copy_source_idle(db, case)
    return cases


async def _workspace_copy_binding(db, cases):
    return {case.id: (case.name, await _copy_case_binding(db, case)) for case in cases}


async def _refresh_workspace_copy_owner(db, source, user_id):
    await db.refresh(source)
    if source.user_id != user_id:
        raise CaseAdminError('工作区不存在', 404)


async def _prepare_workspace_copy_source(db, source, user_id):
    from .async_ops import finish_before_cancel
    from .owner_storage_budget import hold as owner_capacity

    async with owner_capacity(db, user_id):
        async with AsyncExitStack() as locks:
            await locks.enter_async_context(coordinate_workspaces(db, [source.id]))
            await _refresh_workspace_copy_owner(db, source, user_id)
            cases = await _workspace_copy_members(db, locks, source, content_write=False)
            selection = await _workspace_copy_binding(db, cases)
            references = [(case.id, case.s3_prefix) for case in cases]
        await finish_before_cancel(db.commit())
    return selection, {case_id: await _copy_manifest(prefix) for case_id, prefix in references}


async def _workspace_copy_name_free(db, user_id, name):
    existing = await db.scalar(select(Workspace.id).where(Workspace.user_id == user_id, Workspace.name == name).limit(1))
    if existing is not None:
        raise CaseAdminError('同名工作区已存在', 409)


async def copy_workspace(db: AsyncSession, source: Workspace, user: User,
                         name: str, include_cases: bool):
    from .owner_storage_budget import hold as owner_capacity

    name = validate_name(name)
    user_id = user.id
    async with AsyncExitStack() as preparation:
        try:
            prepared = {}
            if include_cases:
                await _workspace_copy_name_free(db, user_id, name)
                selection, manifests = await _prepare_workspace_copy_source(db, source, user_id)
                try:
                    prepared = await _prepare_copies(preparation, db, user, manifests, 'workspace.copy')
                except SourceChanged as error:
                    raise SourceChanged('复制期间源工作区已变化，请重试') from error
            remote = bool(prepared) and storage._use_s3()
            async with owner_capacity(db, user_id):
                if not remote:
                    await case_provision.recover_owner(db, user_id)
                await _workspace_copy_name_free(db, user_id, name)
                async with AsyncExitStack() as locks:
                    await locks.enter_async_context(coordinate_workspaces(db, [source.id]))
                    await _refresh_workspace_copy_owner(db, source, user_id)
                    if include_cases:
                        cases = await _workspace_copy_members(db, locks, source, content_write=True)
                        if await _workspace_copy_binding(db, cases) != selection:
                            raise SourceChanged('复制期间源工作区已变化，请重试')
                        if not prepared:
                            await check_storage_quota(db, user, sum(file.size for manifest in manifests.values()
                                for file in manifest.values()))
                    copied = Workspace(user_id=user_id, name=name)
                    db.add(copied)
                    await db.flush()
                    if include_cases:
                        from .material_library import copy_to_workspace
                        await copy_to_workspace(db, source.id, copied.id)
                        for case in cases:
                            await _copy_case_locked(db, case, copied, user, case.name, manifests[case.id],
                                                    'workspace.copy', prepared=prepared.get(case.id))
                        for case in cases:
                            if prepared:
                                await _verify_copy_versions(case.s3_prefix, manifests[case.id], remote=remote)
                            else:
                                await case_provision.io(partial(verify_source, case.s3_prefix,
                                                               manifests[case.id], exclude=COPY_EXCLUDED))
                await db.commit()
        except BaseException as error:
            await db.rollback()
            if isinstance(error, SourceChanged):
                raise CaseAdminError(str(error), 409) from error
            raise
    return copied
