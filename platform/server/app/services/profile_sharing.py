"""组织共享 provider profile 链接；密文和密钥不进入分享元数据。"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Membership, ProviderProfile
from .. import storage


def _path(org_id: int, profile_id: int) -> str:
    return f"org-provider-profiles/{org_id}-{profile_id}.json"


async def can_access(db: AsyncSession, profile: ProviderProfile, user_id: int) -> bool:
    if profile.org_id is not None:
        return await org_role(db, profile.org_id, user_id) is not None
    if profile.user_id == user_id:
        return True
    rows = (await db.scalars(select(Membership).where(
        Membership.user_id == user_id, Membership.role.in_(["admin", "member", "viewer"])))).all()
    prefix = f"workspaces/{profile.user_id}"
    for membership in rows:
        path = _path(membership.org_id, profile.id)
        if not storage.exists(prefix, path):
            continue
        try:
            link = json.loads(storage.get_bytes(prefix, path))
            if (link.get("schema") == "vibeflow.org-provider-profile.v1" and
                    link.get("enabled") and link.get("org_id") == membership.org_id and
                    link.get("profile_id") == profile.id):
                return True
        except (ValueError, TypeError):
            continue
    return False


async def org_role(db, org_id, user_id):
    return await db.scalar(select(Membership.role).where(
        Membership.org_id == org_id, Membership.user_id == user_id,
        Membership.role.in_(["admin", "member", "viewer"])))


async def set_share(db: AsyncSession, profile: ProviderProfile, org_id: int, enabled: bool):
    data = {"schema": "vibeflow.org-provider-profile.v1", "org_id": org_id,
            "profile_id": profile.id, "enabled": enabled}
    storage.put_text(f"workspaces/{profile.user_id}", _path(org_id, profile.id),
                     json.dumps(data), "application/json")
    return data
