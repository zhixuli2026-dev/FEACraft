"""Recover a registered Docker Codex session after its API owner exits."""
import asyncio
import json

from fastapi import HTTPException
from sqlalchemy import update

from ..config import get_settings
from ..db import SessionLocal
from ..models import NativeCliSession, User
from . import codex_docker, codex_records, docker_acp, process_identity_native
from .async_ops import finish_before_cancel
from .durable_events import json_text, now
from .workflow_state import ExecutionConflict


async def authorized(db, identifier, user_id):
    row = await db.get(NativeCliSession,identifier,populate_existing=True)
    user = await db.get(User,user_id,populate_existing=True)
    if row is None or row.created_by != user_id:
        raise HTTPException(404,'CLI 会话不存在')
    if user is None or user.status != 'active':
        raise HTTPException(403,'CLI 用户已不可用')
    return row, *codex_records.document(row)


def output(row):
    return {'session':codex_records.public(row),'initialization':None,
            'execution_lock_recovered':False,'staging_reconciled':False}


async def recover(identifier,user_id):
    async with SessionLocal() as db:
        row,journal,previous_stop = await authorized(db,identifier,user_id)
        if previous_stop is not None:
            return output(row)
        expected = {key:getattr(row,key) for key in ('version','phase','profile_json','process_json',
            'stop_json','owner_identity_json','owner_token_sha256','owner_id','launch_nonce')}
    prior_owner = json.loads(expected['owner_identity_json'])
    observed = await finish_before_cancel(asyncio.to_thread(process_identity_native.probe,prior_owner))
    if observed != {'state':'exited','reason':'process_exited'}:
        raise ExecutionConflict('recovery_required','Docker CLI 所属进程仍存活或无法确认已退出')
    selected = json.loads(expected['profile_json'])
    if journal['launch'] is None:
        stop = {'schema':codex_records.NOT_STARTED,'launch_nonce':expected['launch_nonce'],
                'owner':prior_owner,'verified':True}
    else:
        runtime = await docker_acp.configured(get_settings())
        if runtime is None or codex_docker.runtime_value(runtime) != selected['docker_runtime']:
            raise ExecutionConflict('recovery_required','无法核对原 Docker CLI 引擎和镜像')
        scope = codex_docker.Transport(runtime,identifier,user_id,expected['launch_nonce'],None,
              selected['credential'],selected['model'] or selected['credential']['default_model'],None)
        scope.registered, scope.container_ids = True, journal['containers']
        stop = await scope.close('owner_exited')
        codex_docker.validate_stop(stop,journal['launch'],journal['containers'])
    async with SessionLocal() as db:
        row,_,prior = await authorized(db,identifier,user_id)
        if prior is not None:
            return output(row)
        found = await db.execute(update(NativeCliSession).where(NativeCliSession.id == identifier,
              NativeCliSession.created_by == user_id,
              *(getattr(NativeCliSession,key) == value for key,value in expected.items())).values(
                  version=expected['version']+1,phase='failed',error='OwnerExited',ended_at=now(),
                  stop_json=json_text(stop)),execution_options={'synchronize_session':False})
        if found.rowcount != 1:
            raise ExecutionConflict('state_conflict','CLI 会话在核验期间发生变化')
        await db.refresh(row)
        codex_records.document(row)
        result = output(row)
        await finish_before_cancel(db.commit())
        return result
