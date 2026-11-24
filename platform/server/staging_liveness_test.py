"""Test-only delays inside real interactive staging, with SQL/lease observations."""
import asyncio
from contextvars import ContextVar
import json
from pathlib import Path
import time


def instrument(source, boundary):
    if boundary is None:
        return source
    assert boundary in {'collect','prepare','apply'}
    return source+'\nfrom staging_liveness_test import install\ninstall(globals(),'+repr(boundary)+')\n'


def install(namespace, boundary):
    from fastapi import Depends
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.deps import get_current_user
    from app.models import NativeSession,NativeVizSession,NativeCliSession,NativeRequest
    from app.routers.workspaces import get_owned_case
    from app.services import staging_sync,s3_file_transactions as remote
    from app.services import terminal_records,viz_records,codex_records,native_request_records
    from app.services.durable_events import lock_case_execution
    for module in (terminal_records,viz_records,codex_records,native_request_records):
        module.LEASE_SECONDS=10
    active=ContextVar('fixture_interactive_sync',default=False)
    root=namespace['root']
    original=staging_sync.sync_authorized
    async def transfer(*args,**kwargs):
        token=active.set(namespace['mode']=='sync_hold')
        try:return await original(*args,**kwargs)
        finally:active.reset(token)
    staging_sync.sync_authorized=transfer
    # Replace the old pre-collection pause; do not stack two barriers.
    staging_sync.collect=namespace['sync']
    def pause():
        if not active.get() or (root/'sync-started').exists():return
        (root/'sync-started').touch()
        deadline=time.monotonic()+45
        while not (root/'release-sync').exists():
            if time.monotonic()>deadline:raise TimeoutError('Interactive staging IO barrier')
            time.sleep(.02)
    module,name=(staging_sync,'collect') if boundary=='collect' else (remote,'_prepare_after' if boundary=='prepare' else '_apply')
    function=getattr(module,name)
    def delayed(*args,**kwargs):
        pause();return function(*args,**kwargs)
    setattr(module,name,delayed)
    models={'terminal':NativeSession,'viz':NativeVizSession,'cli':NativeCliSession,'agent':NativeRequest}
    @namespace['app'].post('/api/v1/fixture/staging-liveness/{kind}/{case_id}')
    async def control(kind:str,case_id:int,user=Depends(get_current_user)):
        model=models[kind]
        async with SessionLocal() as db:
            await get_owned_case(case_id,user,db)
            await lock_case_execution(db,case_id)
            row=await db.scalar(select(model).where(model.case_id==case_id,model.created_by==user.id))
            result={'phase':row.phase,'heartbeat':row.heartbeat_at.isoformat(),'lease':row.lease_expires_at.isoformat()}
            await db.commit()
            return result


async def verify_liveness(api,case_id,kind,boundary):
    if boundary is None:return
    import httpx
    async def observe():
        start=time.monotonic()
        async with httpx.AsyncClient(trust_env=False,timeout=2.5) as client:
            response=await client.post(api.base+f'/api/v1/fixture/staging-liveness/{kind}/{case_id}',
                                       headers={'Authorization':'Bearer '+api.token})
            response.raise_for_status()
        return response.json(),time.monotonic()-start
    try:
        before,first=await observe()
        await asyncio.sleep(12)  # Exceeds the fixture's 10 second lease.
        after,second=await observe()
        assert before['phase']==after['phase']=='active'
        assert after['heartbeat']>before['heartbeat'] and after['lease']>before['lease'],(before,after)
        from datetime import datetime,timezone
        assert datetime.fromisoformat(after['lease']).replace(tzinfo=timezone.utc)>datetime.now(timezone.utc)
        result={'kind':kind,'boundary':boundary,'before':before,'after':after,
                'control_seconds':[first,second],'blocked_seconds':12}
        (api.root/'staging-liveness.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    except BaseException:
        (api.root/'release-sync').touch()
        raise
