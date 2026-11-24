"""配额服务：org/plan 解析 + 各维度配额校验（方案 §subscriptions 骨架版）

配额维度（Plan.quotas JSON 键）：
  viz_concurrent   全局并发可视化会话上限（按用户算例聚合）
  job_concurrent   并发运行作业上限
  llm_msgs_per_day 每日托管 LLM 请求数（UTC）
  storage_mb       存储配额（MB，按用户全部工作区文件大小）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import (AgentEvent, Case, Conversation, Job, Membership, ModelQuotaDay,
                      Organization, Plan, RemoteHpcRun, Subscription, User, Workspace)
from .offline_license import for_org as offline_entitlement

DEFAULT_QUOTAS = {"viz_concurrent": 4, "job_concurrent": 4,
                  "llm_msgs_per_day": 500, "storage_mb": 2048, "seats": 2}
PLAN_CATALOG = {
    "free": {"name": "免费版", "price_cents": 0, "quotas": DEFAULT_QUOTAS},
    "pro": {"name": "专业版", "price_cents": 9900,
            "quotas": {"viz_concurrent": 10, "job_concurrent": 20,
                       "llm_msgs_per_day": 5000, "storage_mb": 10240, "seats": 10}},
    "team": {"name": "团队版", "price_cents": 49900,
             "quotas": {"viz_concurrent": 30, "job_concurrent": 50,
                        "llm_msgs_per_day": 20000, "storage_mb": 102400, "seats": 50}},
}


async def ensure_plan(db: AsyncSession, code: str, name: str, quotas: dict) -> Plan:
    query = select(Plan).where(Plan.code == code)
    plan = await db.scalar(query)
    if plan is not None:
        return plan
    if db.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    await db.execute(insert(Plan).values(code=code, name=name, quotas=json.dumps(quotas))
                     .on_conflict_do_nothing(index_elements=[Plan.code]))
    return (await db.scalars(query)).one()


async def ensure_free_plan(db: AsyncSession) -> Plan:
    return await ensure_plan(db, "free", "免费版", DEFAULT_QUOTAS)


async def ensure_user_org(db: AsyncSession, user) -> Organization:
    """用户保底个人组织+admin 成员+free 订阅（注册与老用户回填共用）"""
    query = (select(Organization).join(Membership).where(Membership.user_id == user.id,
        Membership.role != "disabled", ~Membership.role.like("invited:%")).order_by(Membership.id).limit(1))
    org = await db.scalar(query)
    if org is not None:
        return org
    # Serialize legacy backfill with the same user on SQLite and PostgreSQL.
    owner_id = await db.scalar(update(User).where(User.id == user.id)
                               .values(status=User.status).returning(User.id))
    if owner_id is None:
        raise ValueError("Organization owner is missing")
    org = await db.scalar(query)
    if org is not None:
        return org
    org = Organization(name=f"{user.email} 的空间")
    db.add(org)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=user.id, role="admin"))
    plan = await ensure_free_plan(db)
    db.add(Subscription(org_id=org.id, plan_id=plan.id, status="active"))
    await db.flush()
    return org


async def active_subscription(db, org_id):
    return await db.scalar(select(Subscription).where(
        Subscription.org_id == org_id, Subscription.status == 'active',
        (Subscription.expires_at.is_(None) | (Subscription.expires_at > datetime.now(timezone.utc))))
        .order_by(Subscription.id.desc()).limit(1))


async def effective_entitlement(db, org_id):
    license_status = await offline_entitlement(get_settings(), org_id)
    if license_status is not None:
        if license_status.state == 'active':
            code = license_status.grant.plan
            catalog = PLAN_CATALOG[code]
            plan = await db.scalar(select(Plan).where(Plan.code == code))
            if plan is None:
                plan = Plan(code=code, name=catalog['name'], quotas=json.dumps(catalog['quotas']))
            return plan, license_status
        return await ensure_free_plan(db), license_status
    sub = await active_subscription(db, org_id)
    return (await db.get(Plan, sub.plan_id) if sub else await ensure_free_plan(db)), None


async def effective_plan(db, org_id):
    plan, _ = await effective_entitlement(db, org_id)
    return plan


async def quotas_for(db: AsyncSession, user, *, plan=None) -> dict:
    """用户当前生效配额（个人组织 active 订阅的 plan）"""
    if plan is None:
        org = await ensure_user_org(db, user)
        plan = await effective_plan(db, org.id)
    q = dict(DEFAULT_QUOTAS)
    try:
        q.update(json.loads(plan.quotas or "{}"))
    except Exception:
        pass
    return q


def _over(limit: int, what: str, user=None, *, code=None):
    if user is not None:
        from .eventbus import publish
        publish(user.id, {"type": "quota.warning", "dimension": what, "limit": limit})
    message = f'超出配额：{what}（当前套餐上限 {limit}）。升级套餐或释放资源后再试。'
    raise HTTPException(429, {'code': code, 'message': message} if code else message)


async def check_viz_quota(db: AsyncSession, user, active_count: int):
    q = await quotas_for(db, user)
    if active_count >= q["viz_concurrent"]:
        _over(q["viz_concurrent"], "并发可视化会话", user)


async def running_jobs(db: AsyncSession, owner_id: int):
    local = await db.scalar(
        select(func.count(Job.id))
        .join(Case, Job.case_id == Case.id)
        .join(Workspace, Case.workspace_id == Workspace.id)
        .where(Workspace.user_id == owner_id, Job.status == "running")) or 0
    return local + await occupied_remote_jobs(db,owner_id)


async def occupied_remote_jobs(db: AsyncSession,owner_id: int):
    from .remote_hpc import TERMINAL
    return await db.scalar(select(func.count(RemoteHpcRun.id))
        .join(Case,RemoteHpcRun.case_id==Case.id).join(Workspace,Case.workspace_id==Workspace.id)
        .where(Workspace.user_id==owner_id,RemoteHpcRun.state.not_in(TERMINAL))) or 0


async def check_job_quota(db: AsyncSession, user, *, case_id=None):
    # The submission check and the serialized worker claim bill the workspace
    # owner even when an editor submits on behalf of another workspace.
    owner = user
    if case_id is not None:
        owner = (await db.scalars(select(User).join(Workspace,Workspace.user_id==User.id)
            .join(Case,Case.workspace_id==Workspace.id).where(Case.id==case_id))).one()
    q = await quotas_for(db, owner)
    running = await running_jobs(db, owner.id)
    if (running or 0) >= q['job_concurrent']:
        _over(q['job_concurrent'], '并发作业', user)


async def _daily_breakdown(db: AsyncSession, user_id: int, day) -> dict[str, int]:
    from .conversation_history import event_text_field

    start = datetime.combine(day, dtime.min, tzinfo=timezone.utc)
    # Before the ledger existed, user messages were attributed to the project
    # owner. Keep those events in today's usage when upgrading old databases.
    legacy = await db.scalar(
        select(func.count(AgentEvent.id))
        .join(Conversation, AgentEvent.conversation_id == Conversation.id)
        .join(Case, Conversation.case_id == Case.id)
        .join(Workspace, Case.workspace_id == Workspace.id)
        .where(Workspace.user_id == user_id,
               AgentEvent.type == "message",
               AgentEvent.created_at >= start, AgentEvent.created_at < start + timedelta(days=1),
               event_text_field(db, 'role') == 'user',
               func.coalesce(event_text_field(db, 'history_origin'), '') != 'import',
               func.coalesce(event_text_field(db, 'quota_origin'), '') != 'daily')) or 0
    rows = await db.execute(select(ModelQuotaDay.kind, ModelQuotaDay.used).where(
        ModelQuotaDay.user_id == user_id, ModelQuotaDay.day == day))
    return {'legacy': legacy, **dict(rows.tuples().all())}


async def admit_model(db: AsyncSession, user_id: int, kind: str):
    """Serialize admissions across API processes in the caller's transaction."""
    owner = await db.scalar(update(User).where(User.id == user_id, User.status == 'active')
                            .values(id=User.id).returning(User.id))
    if owner is None:
        raise HTTPException(401, '用户不可用')
    user = await db.get(User, owner)
    day = datetime.now(timezone.utc).date()
    limit = (await quotas_for(db, user))['llm_msgs_per_day']
    if type(limit) is not int or limit < 0:
        raise HTTPException(503, '每日模型配额配置无法核验')
    breakdown = await _daily_breakdown(db, owner, day)
    if sum(breakdown.values()) >= limit:
        _over(limit, '每日 LLM 请求', user, code='model_quota_exceeded')
    row = await db.get(ModelQuotaDay, (owner, day, kind))
    if row is None:
        db.add(ModelQuotaDay(user_id=owner, day=day, kind=kind, used=1))
    else:
        row.used += 1


@dataclass(frozen=True)
class StorageInventory:
    owner_id: int
    revision: int
    bytes: int
    complete: bool


async def prepare_storage_inventory(owner_id,revision,*,stop_after=None):
    measured=await _storage_usage_bytes(owner_id,stop_after=stop_after)
    return StorageInventory(owner_id,revision,measured.bytes,measured.complete)


async def check_storage_quota(db: AsyncSession, user, incoming_bytes: int, *, inventory=None):
    if type(incoming_bytes) is not int or incoming_bytes<0:
        raise HTTPException(422,'存储增量必须是非负整数字节数')
    from .storage_admission import busy,lock_owner
    if inventory is not None and inventory.owner_id!=user.id:raise busy()
    await lock_owner(db,user.id,inventory_revision=inventory.revision if inventory else None)
    await check_locked_storage_quota(db,user,incoming_bytes,inventory=inventory)


async def check_locked_storage_quota(db: AsyncSession, user, incoming_bytes: int, *, inventory=None):
    """Apply the final limit after the caller has secured owner admission."""
    await _locked_storage_quota(db, user, incoming_bytes, inventory)


async def fits_locked_storage_quota(db: AsyncSession, user, incoming_bytes: int) -> bool:
    """Test a conservative batch peak without emitting a quota rejection."""
    return await _locked_storage_quota(db, user, incoming_bytes, None, probe=True)


async def _locked_storage_quota(db, user, incoming_bytes, inventory, *, probe=False):
    from .storage_admission import busy,reserved_bytes
    if type(incoming_bytes) is not int or incoming_bytes<0:
        raise HTTPException(422,'存储增量必须是非负整数字节数')
    if inventory is not None and inventory.owner_id!=user.id:raise busy()
    if inventory is None:
        from .cli_initialization_capacity import reconcile
        await reconcile(db,user.id)
    q = await quotas_for(db, user)
    limit=_storage_limit(q)
    requested=incoming_bytes+await reserved_bytes(db,user.id)
    if requested>limit:
        if probe:
            return False
        await _storage_over(db,user,q)
    measured=inventory if inventory is not None else await _storage_usage(user,stop_after=limit-requested)
    if probe:
        return measured.complete and measured.bytes + requested <= limit
    if measured.bytes+requested>limit:await _storage_over(db,user,q)
    if not measured.complete:raise busy()  # A newer limit may allow more than the lower bound.


async def _storage_over(db,user,quotas):
    # Called while holding the owner admission lock. Classify the open grant
    # atomically with the quota decision; a later probe could miss its settlement.
    from ..models import OwnerStorageReservation
    from .owner_storage_budget import _open_grants, exclusion
    query = _open_grants(user.id).where(OwnerStorageReservation.remaining_bytes > 0)
    excluded=exclusion(db,user.id)
    if excluded is not None:query=query.where(OwnerStorageReservation.id!=excluded)
    if await db.scalar(query.limit(1)) is not None:
        raise HTTPException(409,{'code':'storage_quota_busy','message':'账户的远端写入尚未结算，请稍后重试'})
    _over(quotas["storage_mb"], "存储空间", user)


def _storage_limit(quotas):
    limit=quotas.get('storage_mb')
    if type(limit) is not int or limit<0:raise HTTPException(503,'存储配额配置无法核验')
    return limit*1024*1024


async def _storage_usage(user,*,stop_after=None):
    return await _storage_usage_bytes(user.id,stop_after=stop_after)


async def _storage_usage_bytes(user_id,*,stop_after=None):
    from .storage_usage import measure_user,MeasurementUnavailable
    try:return await measure_user(user_id,stop_after=stop_after)
    except MeasurementUnavailable as error:
        # Record fixed inventory diagnostics and exception types, never raw
        # backend exception text, URLs, credentials or private file contents.
        import logging
        cause=error;chain=[]
        for _ in range(5):
            if cause is None:break
            chain.append(type(cause).__name__);cause=cause.__cause__
        logging.getLogger(__name__).warning('Storage inventory unavailable: %s; cause types=%s',str(error),' -> '.join(chain))
        raise HTTPException(503,'存储用量暂时无法核验，请稍后重试') from error


async def usage(db: AsyncSession, user, *, plan=None) -> dict:
    """当前用量（设置页展示）"""
    q = await quotas_for(db, user, plan=plan)
    running = await running_jobs(db,user.id)
    from .job_delivery import occupied_jobs
    occupied = await occupied_remote_jobs(db,user.id)
    async with occupied_jobs(db,user.id) as jobs:
        async for _ in jobs:
            occupied += 1
    from . import viz_records
    viz_slots, _ = await viz_records.occupied_slots(db,user.id)
    day = datetime.now(timezone.utc).date()
    model_usage = await _daily_breakdown(db, user.id, day)
    _storage_limit(q)
    measured=await _storage_usage(user)
    from .storage_admission import reserved_bytes
    return {"quotas": q, "running_jobs": running, "occupied_job_slots": occupied,
            "occupied_viz_slots":viz_slots,"llm_msgs_today": sum(model_usage.values()),
            "llm_requests_by_kind": model_usage,
            "llm_resets_at": datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=timezone.utc).isoformat(),
            "storage_mb_used": round(measured.bytes / 1024 / 1024, 1),
            "storage_bytes_used":measured.bytes,
            "storage_bytes_reserved":await reserved_bytes(db,user.id)}
