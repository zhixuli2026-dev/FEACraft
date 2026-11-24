"""Commit a new workflow with its explicit engineering reset."""
import asyncio
from contextlib import asynccontextmanager
from functools import partial
import json
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from .. import storage
from ..config import get_settings
from ..models import CaseEventCounter, DurableEvent, User, WorkflowRun, WorkflowStep
from . import engineering_stage, file_transactions, storage_operations
from .case_write import CaseWriteConflict
from .durable_events import json_text
from .workflow_state import ExecutionConflict


class ReplayedReset(Exception):
    pass


async def require_sequence(db, case_id, expected, message):
    """Allow only the reservation and file witness of this exact staged reset."""
    counter = await db.get(CaseEventCounter, case_id, populate_existing=True)
    current = counter.next_seq if counter else 1
    if current == expected:
        return
    lease = storage_operations.current()
    if lease is not None and lease.case.id == case_id and 0 < current - expected <= 2:
        rows = list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id == case_id,
            DurableEvent.seq >= expected, DurableEvent.seq < current).order_by(DurableEvent.seq)))
        wanted = [('storage.operation.started', {'operation_id':lease.id, 'purpose':'staging'})]
        if lease.document_key is not None:
            transaction, digest, witness = lease.document_key
            wanted.append((file_transactions.EVENT, {'transaction_id':transaction, 'plan_sha256':digest}))
        if (len(rows) == len(wanted) == current - expected
                and [(row.type, json.loads(row.payload_json)) for row in rows] == wanted
                and (len(rows) == 1 or rows[-1].id == witness)):
            return
    raise ExecutionConflict('input_stale', message)


async def create_run(db, case, user_id, *, target, run_dir, key, digest, budget, plan, draft, reset_name):
    from .quota import ensure_user_org
    from .workflow_scheduler import case_snapshot

    actor = await db.get(User, user_id)
    org = await ensure_user_org(db, actor)
    reset = {'confirmed_by':user_id, 'from_state':draft.source.workflow_state, 'to_state':'A',
             'from_revision':draft.source.dict_revision, 'to_revision':case.dict_revision}
    dictionary = draft.prepared.files[engineering_stage.CASE_DICT_PATH].decode('utf-8') if draft.prepared else draft.original_text
    snapshot = case_snapshot(case, original_dictionary=dictionary)
    run = WorkflowRun(id=str(uuid.uuid4()), case_id=case.id, org_id=org.id, created_by=user_id,
        target=target, request_hash=digest, idempotency_key=key, expected_revision=case.dict_revision, run_dir=run_dir,
        plan_json=json_text(plan), budget_json=json_text(budget),
        input_manifest_json=json_text(dict(snapshot, **{reset_name:reset})))
    db.add(run)
    await db.flush()
    for item in plan['steps']:
        db.add(WorkflowStep(run_id=run.id, step_key=item['key'], handler=item['handler'],
            handler_version=item['version'], confirmation_state=item['confirmation_state'],
            dependencies_json=json_text(item['dependencies'])))
    await db.flush()
    return run, reset


async def _replayed_result(db, case_id, user_id, key, digest):
    from .workflow_execution import detail
    from .workflow_recompute import replay

    existing = await replay(db, case_id, user_id, key, digest)
    if existing is None:
        return None
    actor = await db.get(User, user_id, populate_existing=True)
    return await detail(db, existing.id, actor)


async def _wait_for_storage(db, case_id, user_id, key, digest, until):
    while time.monotonic() < until:
        result = await _replayed_result(db, case_id, user_id, key, digest)
        if result is not None:
            return result
        active = await storage_operations.active(db, case_id)
        await db.rollback()
        if active is None:
            return None
        await asyncio.sleep(.1)
    raise storage_operations.busy()


async def prepare_case(db, case_id, user_id, key, digest):
    """Reconcile prior file work or return an already durable same-key reset."""
    from ..routers.files import _case_for_file_request

    until = time.monotonic() + get_settings().storage_measurement_timeout_seconds
    while True:
        actor = await db.get(User, user_id, populate_existing=True)
        if actor is None or actor.status != 'active':
            raise HTTPException(403, '用户已不可用')
        try:
            return await _case_for_file_request(case_id, actor, db), None
        except CaseWriteConflict as error:
            if error.code != 'file_sync_busy' or not storage._use_s3():
                raise
            await db.rollback()
            result = await _wait_for_storage(db, case_id, user_id, key, digest, until)
            if result is not None:
                return None, result


async def publish(db, case, user_id, *, key, digest, revision, expected_sha256, validate, create, request):
    from ..routers.files import _locked_recovery_case
    from . import workflow_execution as execution
    from .workflow_recompute import replay

    case_id, workspace_id, prefix = case.id, case.workspace_id, case.s3_prefix
    created_id = committed_id = None
    authorize = partial(_locked_recovery_case, case_id=case_id, user_id=user_id, write=True, prefix=prefix)

    async def checked(session):
        current = await authorize(session)
        if current.workspace_id != workspace_id:
            raise CaseWriteConflict('case_source_changed', '工作流算例来源已变化')
        return current

    @asynccontextmanager
    async def confirmation(session, current, draft):
        nonlocal created_id
        existing = await replay(session, case_id, user_id, key, digest)
        if existing is not None:
            raise ReplayedReset(existing.id)
        await validate(session, current, draft)
        yield
        created_id = await create(session, current, draft)

    def acknowledge(_receipt):
        nonlocal committed_id
        committed_id = created_id

    until = time.monotonic() + get_settings().storage_measurement_timeout_seconds
    while True:
        try:
            await engineering_stage.advance_staged(db, case, checked, target='A', confirmed=True,
                expected_revision=revision, expected_sha256=expected_sha256, confirmation=confirmation,
                defer_journal=True, on_commit=acknowledge, actor_id=user_id)
            break
        except ReplayedReset as error:
            await db.rollback()
            actor = await db.get(User, user_id, populate_existing=True)
            return await execution.detail(db, error.args[0], actor)
        except Exception as error:
            await db.rollback()
            if committed_id is not None:
                raise HTTPException(409, {'code':'workflow_reset_cleanup_pending', 'committed':True,
                    'run_id':committed_id, 'case_id':case_id,
                    'message':'工作流回退已提交，文件清理仍在等待，请读取新工作流状态'}) from error
            if not isinstance(error, engineering_stage.StageAdvanceError):
                raise
            detail = error.detail if isinstance(error.detail, dict) else {'message':str(error.detail)}
            code = detail.get('code', 'state_conflict')
            # A racing same-key S3 request cannot acquire the owner's lease.
            # Return its durable run once it commits and leave dispatch to it.
            if code == 'file_sync_busy' and storage._use_s3():
                result = await _wait_for_storage(db, case_id, user_id, key, digest, until)
                if result is not None:
                    return result
                case = await checked(db)
                continue
            if code in {'file_conflict','case_dict_state_conflict'}:
                raise ExecutionConflict('input_stale', 'Case dictionary changed during reset planning') from error
            if code == 'revision_conflict':
                result = await _replayed_result(db, case_id, user_id, key, digest)
                if result is not None:
                    return result
            raise ExecutionConflict(code, detail['message']) from error

    # Only the request that created the run dispatches it. A repeated request
    # reads the durable result and can use the regular dispatch route if needed.
    actor = await db.get(User, user_id, populate_existing=True)
    await execution.dispatch(db, committed_id, actor, request=request)
    return await execution.detail(db, committed_id, actor)
