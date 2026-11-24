"""Workspace and case authorization shared by routes, workers and event readers."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Case, Membership, Organization, OrganizationResourceGrant as Grant, Workspace
from .durable_events import now

ROLES = {"viewer", "member"}
ACTIVE_ROLES = {"viewer","member","admin"}


def _legacy(workspace):
    from .file_read import FileRead, FileReadConflict
    try:
        selected = FileRead(f'workspaces/{workspace.user_id}',f'workspace-shares/{workspace.id}.json')
        if selected.size>4096:
            return None
        value = json.loads(b''.join(selected.chunks()))
    except (FileNotFoundError,ValueError,TypeError,FileReadConflict):
        return None
    if (not isinstance(value,dict) or value.get('schema')!='vibeflow.workspace-share.v1'
            or value.get('workspace_id')!=workspace.id or value.get('role') not in ROLES
            or type(value.get('enabled')) is not bool or type(value.get('org_id')) is not int or value['org_id']<=0):
        return None
    return value


async def memberships(db, user_id):
    return dict((await db.execute(select(Membership.org_id,Membership.role).where(
        Membership.user_id==user_id, Membership.role.in_(ACTIVE_ROLES)))).all())


async def rows(db, workspace, case=None):
    scope, identifier = ('case',case.id) if case is not None else ('workspace',workspace.id)
    return (await db.scalars(select(Grant).where(Grant.workspace_id==workspace.id, Grant.scope==scope,
        Grant.resource_id==identifier).order_by(Grant.org_id).execution_options(populate_existing=True))).all()


def public(row):
    return {'org_id':row.org_id,'role':row.role,'role_ceiling':row.role_ceiling,'enabled':row.enabled,
            'delegated':row.delegated,'version':row.version}


async def grants(db, workspace, case=None):
    current = await rows(db,workspace,case)
    if current or case is not None:
        return [public(row) for row in current]
    previous = await asyncio.to_thread(_legacy,workspace)
    return [] if previous is None else [{**previous,'role_ceiling':previous['role'],
                                         'delegated':False,'version':0}]


def strongest(grants, member_roles):
    admitted = set()
    for grant in grants:
        role = member_roles.get(grant['org_id'])
        if grant['enabled'] and role is not None:
            admitted.add('viewer' if role=='viewer' else grant['role'])
    return 'member' if 'member' in admitted else 'viewer' if admitted else None


async def access_role(db: AsyncSession, workspace: Workspace, user_id: int,
                      write: bool = False, *, member_roles=None) -> str | None:
    if workspace.user_id == user_id:
        return 'owner'
    member_roles = await memberships(db,user_id) if member_roles is None else member_roles
    if not member_roles:
        return None
    role = strongest(await grants(db,workspace),member_roles)
    return None if write and role!='member' else role


async def case_access_role(db, workspace, case, user_id, *, write=False):
    if workspace.user_id==user_id:
        return 'owner'
    member_roles = await memberships(db,user_id)
    if not member_roles:
        return None
    inherited = await access_role(db,workspace,user_id,member_roles=member_roles)
    if inherited=='member':
        return inherited
    explicit = strongest(await grants(db,workspace,case),member_roles)
    role = explicit or inherited
    return None if write and role!='member' else role


async def case_grant_ids(db, workspace, member_roles):
    if not member_roles:
        return set()
    return set(await db.scalars(select(Grant.case_id).join(Case,Case.id==Grant.case_id).where(
        Grant.scope=='case', Grant.workspace_id==workspace.id, Case.workspace_id==workspace.id,
        Grant.enabled.is_(True), Grant.org_id.in_(member_roles))))


async def visible_case_ids(db,workspace,user_id):
    """None identifies workspace-wide access. A set contains individually shared cases."""
    if workspace.user_id==user_id:
        return None
    member_roles = await memberships(db,user_id)
    if await access_role(db,workspace,user_id,member_roles=member_roles) is not None:
        return None
    return await case_grant_ids(db,workspace,member_roles)


async def view_grants(db,workspace,case,user_id):
    result = await grants(db,workspace,case)
    if workspace.user_id==user_id:
        return {'can_manage':True,'owner':True,'grants':result}
    roles = await memberships(db,user_id)
    result = [item for item in result if item['delegated'] and roles.get(item['org_id'])=='admin']
    return {'can_manage':bool(result),'owner':False,'grants':result}


def new_row(scope, identifier, org_id, workspace, case, role, caller):
    return Grant(scope=scope,resource_id=identifier,org_id=org_id,workspace_id=workspace.id,
                 case_id=case.id if case is not None else None,role=role,role_ceiling=role,enabled=False,
                 delegated=False,legacy_slot=False,version=1,updated_by=caller,updated_at=now())


async def legacy_status(db,workspace):
    found = await rows(db,workspace)
    if not found:
        return await asyncio.to_thread(_legacy,workspace)
    row = next((item for item in found if item.legacy_slot),None)
    if row is None:
        return None
    return {'schema':'vibeflow.workspace-share.v1','workspace_id':workspace.id,'org_id':row.org_id,
            'role':row.role,'enabled':row.enabled}


async def update_grant(db,workspace,case,actor,org_id,role,enabled,*,delegated=None,expected=None,legacy=False):
    """Caller holds workspace coordination and, when present, the case lock."""
    from fastapi import HTTPException
    from . import audit
    scope, identifier = ('case',case.id) if case is not None else ('workspace',workspace.id)
    owner = workspace.user_id==actor.id
    if not owner and (legacy or (await memberships(db,actor.id)).get(org_id)!='admin'):
        raise HTTPException(404,'授权不存在')
    if await db.get(Organization,org_id) is None:
        raise HTTPException(404,'组织不存在')
    current = await rows(db,workspace,case)
    if scope=='workspace' and owner and not current:
        item = await asyncio.to_thread(_legacy,workspace)
        if item is not None and await db.get(Organization,item['org_id']) is not None:
            prior=new_row(scope,identifier,item['org_id'],workspace,case,item['role'],workspace.user_id)
            prior.enabled,prior.legacy_slot=item['enabled'],True
            db.add(prior)
            current.append(prior)
    row = next((item for item in current if item.org_id==org_id),None)
    if not owner and (row is None or not row.delegated or delegated is not None
                      or (role=='member' and row.role_ceiling!='member')):
        raise HTTPException(403,'组织管理员只能在所有者委托范围内管理授权')
    version = row.version if row is not None else 0
    if expected is not None and not (expected==version or expected==0 and row is not None and row in db.new):
        raise HTTPException(409,{'code':'acl_revision_changed','message':'授权已变化，请重新读取'})
    if row is None:
        # A case grant bound to its former workspace becomes inactive on a move.
        row = await db.get(Grant,(scope,identifier,org_id))
        if row is None:
            row = new_row(scope,identifier,org_id,workspace,case,role,actor.id)
            db.add(row)
        else:
            row.workspace_id = workspace.id
            row.version += 1
    else:
        row.version += 1
    row.role,row.enabled,row.updated_by,row.updated_at=role,enabled,actor.id,now()
    if owner:
        row.role_ceiling=role
        if delegated is not None:
            row.delegated=delegated
    if legacy:
        for prior in current:
            if prior is not row and prior.legacy_slot:
                prior.legacy_slot,prior.enabled=False,False
                prior.version+=1
                prior.updated_by,prior.updated_at=actor.id,now()
                await audit.append(db,prior.org_id,actor.id,'organization.access_grant.changed',scope,identifier,
                    {'enabled':False,'role':prior.role,'delegated':prior.delegated})
        row.legacy_slot=True
    await audit.append(db,org_id,actor.id,'organization.access_grant.changed',scope,identifier,
        {'role':row.role,'enabled':row.enabled,'delegated':row.delegated,'version':row.version})
    await db.flush()
    return public(row)
