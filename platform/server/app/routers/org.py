"""组织/订阅：配额、成员邀请、seat、角色与禁用。"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import Membership, Organization, Plan, Subscription, User
from ..services import audit
from ..services.quota import DEFAULT_QUOTAS, PLAN_CATALOG as _CATALOG, active_subscription, effective_entitlement
from ..services.quota import effective_plan, ensure_plan, ensure_user_org, usage

router = APIRouter(prefix="/api/v1/org", tags=["org"])
ACTIVE_ROLES = {"admin", "member", "viewer"}


class InviteIn(BaseModel):
    email: str
    role: str = "member"


class RoleIn(BaseModel):
    role: str


class CheckoutIn(BaseModel):
    plan_code: str
    period: str = "monthly"
    confirmed: bool = False


class RenewIn(BaseModel):
    period: str = "monthly"


def _period_days(period: str) -> int:
    if period not in {"monthly", "yearly"}:
        raise HTTPException(422, "period 只能是 monthly/yearly")
    return 30 if period == "monthly" else 365


def _billing_changes_allowed():
    if get_settings().offline_license_bundle_path:
        raise HTTPException(409, {'code':'offline_license_managed','message':'离线部署由许可证签发方管理套餐'})


async def _ensure_catalog(db: AsyncSession) -> list[Plan]:
    return [await ensure_plan(db, code, item["name"], item["quotas"]) for code, item in _CATALOG.items()]


async def _expire_subscriptions(db: AsyncSession, org_id: int):
    now = datetime.now(timezone.utc)
    expired = (
        Subscription.org_id == org_id, Subscription.status == "active",
        Subscription.expires_at.is_not(None), Subscription.expires_at <= now)
    if await db.scalar(select(Subscription.id).where(*expired).limit(1)) is not None:
        await db.execute(update(Subscription).where(*expired).values(status="expired"))
        await db.commit()


def _subscription_out(subscription: Subscription, plan: Plan) -> dict:
    return {"id": subscription.id, "status": subscription.status,
            "plan_code": plan.code, "plan_name": plan.name,
            "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
            "created_at": subscription.created_at.isoformat()}


async def _org(db: AsyncSession, org_id: int) -> Organization:
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "组织不存在")
    return org


async def _membership(db: AsyncSession, org_id: int, user_id: int) -> Membership | None:
    return await db.scalar(select(Membership).where(
        Membership.org_id == org_id, Membership.user_id == user_id))


async def _require_admin(db: AsyncSession, org_id: int, user: User) -> Organization:
    org = await _org(db, org_id)
    membership = await _membership(db, org_id, user.id)
    if membership is None or membership.role != "admin":
        raise HTTPException(403, "仅组织 admin 可管理成员")
    return org


@asynccontextmanager
async def _org_mutation(db: AsyncSession, org_id: int, *, admin: User | None = None):
    # Use the same transaction for seat decisions across API processes.
    locked = await db.scalar(update(Organization).where(Organization.id == org_id)
                             .values(name=Organization.name).returning(Organization.id))
    if locked is None:
        raise HTTPException(404, "组织不存在")
    org = await (_require_admin(db, org_id, admin) if admin is not None else _org(db, org_id))
    yield org


def _member_out(membership: Membership, user: User) -> dict:
    role = membership.role
    return {"user_id": user.id, "email": user.email, "name": user.name,
            "role": role.split(":", 1)[1] if role.startswith("invited:") else role,
            "status": "invited" if role.startswith("invited:") else role,
            "account_status": user.status}


async def _seat_limit(db: AsyncSession, org_id: int) -> int:
    plan = await effective_plan(db, org_id)
    quotas = dict(DEFAULT_QUOTAS)
    if plan is not None:
        try:
            quotas.update(json.loads(plan.quotas or "{}"))
        except (TypeError, ValueError):
            pass
    limit = quotas.get("seats", 2)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise HTTPException(500, "组织 seat 配额配置无效")
    return limit


async def _seats(db: AsyncSession, org_id: int) -> dict:
    roles = (await db.scalars(select(Membership.role).where(Membership.org_id == org_id))).all()
    active = sum(role in ACTIVE_ROLES for role in roles)
    invited = sum(role.startswith("invited:") for role in roles)
    return {"used": active + invited, "limit": await _seat_limit(db, org_id),
            "active": active, "invited": invited}


@router.get("/mine")
async def my_org(user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)):
    org = await ensure_user_org(db, user)
    await db.commit()
    plan, license_status = await effective_entitlement(db, org.id)
    members = (await db.scalars(
        select(Membership).where(Membership.org_id == org.id))).all()
    u = await usage(db, user, plan=plan)
    return {
        "org": {"id": org.id, "name": org.name},
        "plan": {"code": plan.code, "name": plan.name} if plan else None,
        "offline_license": license_status.public() if license_status else None,
        "members": [{"user_id": m.user_id, "role": m.role} for m in members],
        "usage": u,
        "quotas_human": {
            "并发可视化会话": u["quotas"]["viz_concurrent"],
            "并发作业": u["quotas"]["job_concurrent"],
            "每日 LLM 消息": u["quotas"]["llm_msgs_per_day"],
            "存储(MB)": u["quotas"]["storage_mb"],
        },
    }


@router.get("/plans")
async def plan_catalog(user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    plans = await _ensure_catalog(db)
    await db.commit()
    return [{"code": plan.code, "name": plan.name,
             "price_cents": _CATALOG[plan.code]["price_cents"],
             "quotas": json.loads(plan.quotas)} for plan in plans]


@router.get("/memberships")
async def membership_directory(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(Organization.id,Organization.name,Membership.role).join(
        Membership,Membership.org_id==Organization.id).where(Membership.user_id==user.id,
        Membership.role.in_(ACTIVE_ROLES)).order_by(Organization.name,Organization.id))
    return [{'id':org_id,'name':name,'role':role} for org_id,name,role in rows]


@router.get("/{org_id}/billing")
async def billing_status(org_id: int, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    await _require_admin(db, org_id, user)
    if get_settings().offline_license_bundle_path:
        plan, license_status = await effective_entitlement(db, org_id)
        return {'provider':'offline','charge':False,'current':None,'history':[],
                'effective_plan':plan.code,'offline_license':license_status.public()}
    await _expire_subscriptions(db, org_id)
    current = await active_subscription(db, org_id)
    current_plan = await db.get(Plan, current.plan_id) if current else None
    rows = (await db.scalars(select(Subscription).where(Subscription.org_id == org_id)
                             .order_by(Subscription.id.desc()).limit(50))).all()
    history = []
    for row in rows:
        plan = await db.get(Plan, row.plan_id)
        history.append(_subscription_out(row, plan))
    return {"provider": "mock", "charge": False,
            "current": _subscription_out(current, current_plan) if current else None,
            "history": history}


@router.post("/{org_id}/billing:mock-checkout")
async def mock_checkout(org_id: int, body: CheckoutIn,
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id, admin=user):
        _billing_changes_allowed()
        if not body.confirmed:
            raise HTTPException(400, "mock checkout 需要 confirmed=true；不会真实扣款")
        if body.plan_code not in _CATALOG:
            raise HTTPException(404, "套餐不存在")
        days = _period_days(body.period)
        plans = await _ensure_catalog(db)
        target = next(plan for plan in plans if plan.code == body.plan_code)
        seats = await _seats(db, org_id)
        target_seats = _CATALOG[body.plan_code]["quotas"]["seats"]
        if seats["used"] > target_seats:
            raise HTTPException(409, f"当前成员+邀请为 {seats['used']}，超过目标套餐 seats={target_seats}")
        current = await active_subscription(db, org_id)
        previous_code = None
        if current:
            previous = await db.get(Plan, current.plan_id)
            previous_code = previous.code
            current.status = "superseded"
        subscription = Subscription(org_id=org_id, plan_id=target.id, status="active",
                                    expires_at=datetime.now(timezone.utc) + timedelta(days=days))
        db.add(subscription)
        await db.flush()
        payment_id = "mock_" + uuid.uuid4().hex
        await audit.append(db, org_id, user.id, "billing.subscription.changed", "subscription", subscription.id,
                           {"previous_plan": previous_code, "plan_code": body.plan_code,
                            "period": body.period, "mock_payment_id": payment_id})
        await db.commit()
    return {"provider": "mock", "charge": False, "mock_payment_id": payment_id,
            "subscription": _subscription_out(subscription, target)}


@router.post("/{org_id}/billing:renew")
async def renew_subscription(org_id: int, body: RenewIn,
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id, admin=user):
        _billing_changes_allowed()
        days = _period_days(body.period)
        current = await active_subscription(db, org_id)
        if current is None:
            raise HTTPException(404, "没有生效中的订阅")
        now = datetime.now(timezone.utc)
        expires_at = current.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        base = max(now, expires_at or now)
        current.expires_at = base + timedelta(days=days)
        plan = await db.get(Plan, current.plan_id)
        await audit.append(db, org_id, user.id, "billing.subscription.renewed", "subscription", current.id,
                           {"plan_code": plan.code, "period": body.period})
        await db.commit()
        return {"provider": "mock", "charge": False,
                "subscription": _subscription_out(current, plan)}


@router.post("/{org_id}/billing:cancel")
async def cancel_subscription(org_id: int, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id, admin=user):
        _billing_changes_allowed()
        current = await active_subscription(db, org_id)
        if current is None:
            raise HTTPException(404, "没有生效中的订阅")
        current.status = "cancelled"
        plan = await db.get(Plan, current.plan_id)
        await audit.append(db, org_id, user.id, "billing.subscription.cancelled", "subscription", current.id,
                           {"plan_code": plan.code})
        await db.commit()
        return {"ok": True, "subscription": _subscription_out(current, plan)}


@router.get("/invitations")
async def invitations(user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Membership, Organization).join(
        Organization, Membership.org_id == Organization.id).where(
        Membership.user_id == user.id, Membership.role.like("invited:%"))
        .order_by(Membership.id))).all()
    return [{"org_id": org.id, "org_name": org.name,
             "role": membership.role.split(":", 1)[1]} for membership, org in rows]


@router.get("/{org_id}/audit")
async def organization_audit(org_id: int, action: str | None = Query(None, max_length=80),
                             actor_user_id: int | None = Query(None, ge=1),
                             offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    await _require_admin(db, org_id, user)
    return await audit.query(db, org_id, action=action, actor_user_id=actor_user_id,
                             offset=offset, limit=limit)


@router.get("/{org_id}/members")
async def list_members(org_id: int, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    org = await _require_admin(db, org_id, user)
    rows = (await db.execute(select(Membership, User).join(
        User, Membership.user_id == User.id).where(Membership.org_id == org_id)
        .order_by(Membership.id))).all()
    return {"org": {"id": org.id, "name": org.name}, "seats": await _seats(db, org_id),
            "members": [_member_out(membership, member) for membership, member in rows]}


@router.post("/{org_id}/members", status_code=201)
async def invite_member(org_id: int, body: InviteIn,
                        user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id, admin=user) as org:
        if body.role not in ACTIVE_ROLES:
            raise HTTPException(422, "role 只能是 admin/member/viewer")
        email = body.email.strip().lower()
        if not email:
            raise HTTPException(422, "邮箱不能为空")
        member = await db.scalar(select(User).where(func.lower(User.email) == email))
        if member is None:
            raise HTTPException(404, "用户尚未注册，请先注册后再邀请")
        membership = await _membership(db, org_id, member.id)
        if membership is not None and membership.role != "disabled":
            raise HTTPException(409, "用户已有成员资格或待接受邀请")
        seats = await _seats(db, org_id)
        if seats["used"] >= seats["limit"]:
            raise HTTPException(429, f"组织 seat 已满（{seats['limit']}）")
        if membership is None:
            membership = Membership(org_id=org_id, user_id=member.id, role=f"invited:{body.role}")
            db.add(membership)
        else:
            membership.role = f"invited:{body.role}"
        await audit.append(db, org_id, user.id, "org.member.invited", "membership", member.id,
                           {"target_user_id": member.id, "role": body.role})
        await db.commit()
        return {"org": {"id": org.id, "name": org.name},
                "membership": _member_out(membership, member) | {"role": f"invited:{body.role}"},
                "seats": await _seats(db, org_id)}


@router.post("/{org_id}/invitation:accept")
async def accept_invitation(org_id: int, user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id):
        membership = await _membership(db, org_id, user.id)
        if membership is None or not membership.role.startswith("invited:"):
            raise HTTPException(404, "没有待接受邀请")
        seats = await _seats(db, org_id)
        if seats["active"] >= seats["limit"]:
            raise HTTPException(429, "组织 seat 已满")
        membership.role = membership.role.split(":", 1)[1]
        await audit.append(db, org_id, user.id, "org.member.accepted", "membership", user.id,
                           {"role": membership.role})
        await db.commit()
        return {"ok": True, "org_id": org_id, "role": membership.role}


@router.post("/{org_id}/invitation:decline")
async def decline_invitation(org_id: int, user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id):
        membership = await _membership(db, org_id, user.id)
        if membership is None or not membership.role.startswith("invited:"):
            raise HTTPException(404, "没有待接受邀请")
        await db.delete(membership)
        await audit.append(db, org_id, user.id, "org.member.declined", "membership", user.id)
        await db.commit()
        return {"ok": True, "org_id": org_id}


@router.delete("/{org_id}/members/{member_id}")
async def revoke_invitation(org_id: int, member_id: int,
                            user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id, admin=user):
        membership = await _membership(db, org_id, member_id)
        if membership is None:
            raise HTTPException(404, "成员不存在")
        if not membership.role.startswith("invited:"):
            raise HTTPException(409, "active/disabled 成员请使用角色接口禁用，只有邀请可撤销")
        await db.delete(membership)
        await audit.append(db, org_id, user.id, "org.invitation.revoked", "membership", member_id,
                           {"target_user_id": member_id})
        await db.commit()
        return {"ok": True, "seats": await _seats(db, org_id)}


@router.patch("/{org_id}/members/{member_id}")
async def change_member_role(org_id: int, member_id: int, body: RoleIn,
                             user: User = Depends(get_current_user),
                             db: AsyncSession = Depends(get_db)):
    async with _org_mutation(db, org_id, admin=user):
        if body.role not in ACTIVE_ROLES | {"disabled"}:
            raise HTTPException(422, "role 只能是 admin/member/viewer/disabled")
        if member_id == user.id:
            raise HTTPException(409, "不能在控制台修改自己的角色或禁用自己")
        membership = await _membership(db, org_id, member_id)
        if membership is None:
            raise HTTPException(404, "成员不存在")
        if membership.role.startswith("invited:"):
            raise HTTPException(409, "待接受邀请不能改角色，可让对方拒绝后重新邀请")
        if membership.role == "admin" and body.role != "admin":
            admins = await db.scalar(select(func.count(Membership.id)).where(
                Membership.org_id == org_id, Membership.role == "admin"))
            if (admins or 0) <= 1:
                raise HTTPException(409, "组织必须保留至少一个 active admin")
        if membership.role == "disabled" and body.role in ACTIVE_ROLES:
            seats = await _seats(db, org_id)
            if seats["used"] >= seats["limit"]:
                raise HTTPException(429, "组织 seat 已满，不能重新启用")
        previous_role = membership.role
        membership.role = body.role
        member = await db.get(User, member_id)
        await audit.append(db, org_id, user.id, "org.member.role_changed", "membership", member_id,
                           {"target_user_id": member_id, "previous_role": previous_role, "role": body.role})
        await db.commit()
        return {"membership": _member_out(membership, member),
                "seats": await _seats(db, org_id)}
