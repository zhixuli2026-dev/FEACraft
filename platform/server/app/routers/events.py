"""Committed case events and session-local notifications over WebSocket."""
import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..db import get_db, SessionLocal
from ..deps import connection_user_id
from ..models import User
from ..services.case_file_watch import POLL_SECONDS, monitor_case
from ..services.eventbus import ACCOUNT_NOTICES, case_wakeup, subscribe, unsubscribe
from .workspaces import get_owned_case

router = APIRouter(tags=["events"])


async def _wait_disconnect(ws: WebSocket):
    while True:
        message = await ws.receive()
        if message['type'] == 'websocket.disconnect':
            return


async def _forward_events(ws: WebSocket, queue: asyncio.Queue, send_lock: asyncio.Lock, case_id: int, uid: int):
    try:
        while True:
            event = await queue.get()
            # Durable messages come from the database in sequence. Only the
            # explicit account notices can reach more than one case socket.
            event_case = event.get('case_id')
            account_notice = event_case is None and event.get('type') in ACCOUNT_NOTICES
            if event.get('event_id') or (event_case != case_id and not account_notice):
                continue
            async with SessionLocal() as db:
                user = await db.get(User, uid)
                if user is None or user.status != 'active':
                    async with send_lock:
                        await ws.close(code=4401)
                    return
                try:
                    await get_owned_case(case_id, user, db, write=False)
                except HTTPException:
                    async with send_lock:
                        await ws.close(code=4403)
                    return
            async with send_lock:
                await ws.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return


async def _replay_events(ws, case_id, uid, after, send_lock, signal):
    from ..services.event_replay import page
    while True:
        signal.clear()
        async with SessionLocal() as db:
            user = await db.get(User,uid)
            if user is None or user.status != 'active':
                await ws.close(code=4401)
                return
            try:
                await get_owned_case(case_id,user,db,write=False)
            except HTTPException:
                await ws.close(code=4403)
                return
            try:
                batch = await page(db,case_id,after)
            except ValueError:
                async with send_lock:
                    await ws.send_json({'type':'cursor.reset','case_id':case_id,'after':0})
                await ws.close(code=4409)
                return
        for event in batch['events']:
            async with send_lock:await ws.send_json(event)
        after = batch['next_after']
        if not batch['has_more']:
            try:
                await asyncio.wait_for(signal.wait(), timeout=.5)
            except TimeoutError:
                pass


@router.websocket("/ws/events")
async def case_events(ws: WebSocket, case_id: int, token: str = "", after: int | None = None):
    uid = await connection_user_id(ws, token, scope='events', target=str(case_id))
    if uid is None:
        await ws.close(code=4401)
        return
    if after is not None and not 0 <= after <= 2**53-1:
        await ws.close(code=4400)
        return
    db_gen = get_db()
    db = await anext(db_gen)
    try:
        user = await db.get(User, uid)
        if user is None or user.status != "active":
            await ws.close(code=4401)
            return
        try:
            case = await get_owned_case(case_id, user, db,write=False)
            prefix = case.s3_prefix
        except Exception:
            await ws.close(code=4403)
            return
        from sqlalchemy import func, select
        from ..models import DurableEvent
        if after is None:
            after = await db.scalar(select(func.max(DurableEvent.seq)).where(DurableEvent.case_id==case_id)) or 0
    finally:
        await db_gen.aclose()
    await ws.accept()
    queue = subscribe(uid)
    tasks = []
    try:
        with case_wakeup(case_id) as signal:
            async with monitor_case(case_id, prefix) as native:
                try:
                    await ws.send_json({"type": "ready", "case_id": case_id, "backend": "poll", "interval": POLL_SECONDS,
                        "durable_cursor": after, "native_notifications": native})
                    send_lock = asyncio.Lock()
                    tasks = [asyncio.create_task(_forward_events(ws, queue, send_lock, case_id, uid)),
                        asyncio.create_task(_replay_events(ws,case_id,uid,after,send_lock,signal)),
                        asyncio.create_task(_wait_disconnect(ws))]
                    done, _ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        await task
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe(uid, queue)
