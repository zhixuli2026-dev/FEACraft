"""终端服务：PTY over WebSocket（多会话版）

协议（与桌面版 terminalBridge 语义对齐，文本帧 JSON 控制 + 二进制帧字节流）：
  client→server 文本帧: {"type":"input","data":"..."} | {"type":"resize","cols":c,"rows":r}
                        | {"type":"ready","cols":c,"rows":r}
  server→client 二进制帧: PTY 输出原始字节
  server→client 文本帧: {"type":"ready","session":id} | {"type":"exit","code":n} | {"type":"error",...}

多会话语义（对齐桌面版终端多标签+重连）：
  - 不带 session 参数连接 → 新建 PTY；带 ?session=<id> → 挂接已有会话（先补发 backlog）
  - WS 断开不再杀 PTY（shell 在后台继续跑）→ 切标签=重连，进程保持
  - 显式关闭：DELETE /api/v1/terminal/sessions/{id}
  - shell 退出后会话标记 dead（挂接可回看 backlog）

Windows cmd 使用 ConPTY；WSL 使用所属 namespace 内的 Linux PTY。
控制消息与终端字节经独立管道传输，登记根及实际 PTY gate 后才释放命令解释器。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from threading import BoundedSemaphore
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from ..config import get_settings
from ..deps import bearer, connection_user_id, get_current_user
from ..deadline_routes import ImageCleanupDeadlineRoute, SandboxRecoveryDeadlineRoute, StagingCleanupDeadlineRoute
from ..models import DurableEvent, User, NativeSession
from ..services.connection_tickets import terminal_target
from ..db import SessionLocal
from ..services.async_ops import finish_before_cancel
from ..services.case_write import CaseWriteConflict
from ..services.case_write_capacity import committing
from ..services.event_replay import latest_sequence
from ..services.request_deadline import scope as deadline_scope
from ..services.supervisor import register as supervisor_register, unregister as supervisor_unregister

from ..services.terminal_sessions import ClosedTerminal, RESOURCE_LIMIT_MESSAGES, TerminalSession
from ..services.terminal_output import SEND_TIMEOUT,CLOSE_TIMEOUT
from ..services import terminal_completion, terminal_records, terminal_routing
from sqlalchemy import select

router = APIRouter(tags=["terminal"])

_sessions: dict[str, TerminalSession] = {}
_close_requests: dict[str, asyncio.Task] = {}
_reaper_started = False
_reaper_task: asyncio.Task | None = None
_staging_slots = BoundedSemaphore(2)
IDLE_TIMEOUT_S = 900
REAPER_INTERVAL_S = 60


class TerminalStagingCleanupError(OSError):
    pass


def _cleanup_staging(stage_prefix,*,synchronized=False):
    from ..services.staging_cleanup import clean
    try:
        clean(stage_prefix, synchronized=synchronized, retry_seconds=5)
    except TimeoutError:
        raise
    except OSError as error:
        raise TerminalStagingCleanupError('Terminal staging cleanup did not finish') from error


async def _reaper():
    while True:
        await asyncio.sleep(REAPER_INTERVAL_S)
        now = time.time()
        for sid, session in list(_sessions.items()):
            if session.alive is False or (not session._subscribers and now-session.last_activity>IDLE_TIMEOUT_S):
                try:
                    await _stop_terminal(session)
                except Exception as error:
                    print('[WARN] terminal close failed: '+type(error).__name__)


async def _authorize_terminal(db, case_id, user_id):
    """Preserve owner-only shell access; a supplied invalid case never falls back."""
    from ..models import Case, Workspace
    from sqlalchemy import select
    user = await db.get(User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(403, "终端用户已不可用")
    if not case_id:
        return None
    case = await db.scalar(select(Case).join(Workspace).where(
        Case.id == case_id, Workspace.user_id == user_id))
    if case is None:
        raise HTTPException(404, "终端算例不存在或无访问权限")
    return case


@asynccontextmanager
async def _terminal_access(case_id, user_id, *, transaction=False):
    async with SessionLocal() as db:
        case = await _authorize_terminal(db, case_id, user_id)
        if case is None:
            yield (db, None) if transaction else None
            await finish_before_cancel(db.commit())
        else:
            # Wait for an in-flight owner settlement before taking the case lock.
            async with committing(db, case, wait_for_quota=True):
                yield (db, case) if transaction else case


def _capacity_failure(error):
    return isinstance(error, HTTPException) and (error.status_code in {429, 503}
        or error.status_code == 409 and isinstance(error.detail, dict)
        and error.detail.get('code') == 'storage_quota_busy')


async def _new_session(case_id, user_id, cols, rows, shell, provider_profile_id=None, cli_harness=''):
    from ..storage import _use_s3
    staging = bool(case_id and _use_s3())
    if staging and not _staging_slots.acquire(blocking=False):
        raise HTTPException(429, {'code':'terminal_preparation_busy', 'message':'终端工作副本正在准备，请稍后重试'})
    resources={}
    try:
        return await _create_session(case_id,user_id,cols,rows,shell,resources,provider_profile_id,cli_harness)
    except BaseException as error:
        session = resources.get('session')
        can_discard = session is None or (not session.release_attempted and
            (session.transport.owned.proc is None or session.cleanup_verified is True))
        if resources.get('stage') is not None and can_discard:
            try:await finish_before_cancel(asyncio.to_thread(_cleanup_staging,resources['stage']))
            except BaseException as cleanup_error:error.add_note('Unbound terminal staging cleanup: '+type(cleanup_error).__name__)
        raise
    finally:
        if staging:_staging_slots.release()


async def _staging_binding(db,case):
    return case.s3_prefix,case.dict_revision,await latest_sequence(db,case.id)


async def _staging_changed(db,case,binding):
    if (case.s3_prefix,case.dict_revision)!=binding[:2]:return True
    events=(await db.execute(select(DurableEvent.type,DurableEvent.payload_json)
        .where(DurableEvent.case_id==case.id,DurableEvent.seq>binding[2])
        .order_by(DurableEvent.seq).limit(2))).all()
    if not events:return False
    if len(events)!=1 or events[0][0]!='files.changed':return True
    # A newly attached tree watcher records its baseline without writing files.
    try:payload=json.loads(events[0][1])
    except (TypeError,ValueError):return True
    return not isinstance(payload,dict) or payload.get('source')!='file_watch' or payload.get('initial') is not True


async def _create_session(case_id, user_id, cols, rows, shell, resources,provider_profile_id=None,cli_harness=''):
    from ..storage import _fs_path, _use_s3, prepare_job_staging
    from ..services.runtime_config import get_runtime_setting
    from ..services import rate_limit, terminal_credentials
    binding=None;staging_prefix=None
    async def reauthorize():
        async with _terminal_access(case_id,user_id,transaction=True) as (db,current):
            if ((current.s3_prefix if current is not None else None)!=source_prefix or
                    binding is not None and await _staging_changed(db,current,binding)):
                raise CaseWriteConflict('input_stale','终端准备期间算例已变化，请重新打开')
        if credential is not None:
            async with SessionLocal() as db:await terminal_credentials.check(db,credential,user_id)
    try:
        async with deadline_scope(get_settings().terminal_prepare_timeout_seconds) as deadline:
            async with SessionLocal() as db:
                await _authorize_terminal(db,case_id,user_id)
                credential,secret=await terminal_credentials.select(db,provider_profile_id,user_id,cli_harness,shell)
            if credential is not None:
                try:
                    await rate_limit.provider(user_id, credential['profile_id'])
                except rate_limit.RateLimited as error:
                    raise HTTPException(429, {'code': 'provider_rate_limited', 'retry_after': error.retry_after,
                                              'message': '模型配置的调用过于频繁，请稍后重试'}) from error
                except rate_limit.LimiterUnavailable as error:
                    raise HTTPException(503, {'code': 'rate_limiter_unavailable', 'retry_after': 1,
                                              'message': '暂时无法核验模型调用额度'}) from error
            environment=(await finish_before_cancel(asyncio.to_thread(terminal_credentials.environment,credential,secret))
                         if credential is not None else None)
            secret=None
            async with _terminal_access(case_id,user_id,transaction=True) as (db,case):
                if shell=='wsl-sandbox' and case is None:raise HTTPException(400,'沙箱终端需要绑定算例')
                source_prefix=case.s3_prefix if case is not None else None
                if case is not None and _use_s3():binding=await _staging_binding(db,case)
            if binding is not None:
                def prepare():
                    stage=prepare_job_staging(source_prefix,time.time_ns())
                    resources['stage']=stage
                    return stage
                staging_prefix=await finish_before_cancel(asyncio.to_thread(prepare))
                deadline.check()
                await reauthorize()
            work_prefix=staging_prefix or source_prefix
            cwd=str(_fs_path(work_prefix,'.')) if work_prefix is not None else None
            deadline.check()
    except TimeoutError as error:
        raise HTTPException(504, {'code':'terminal_preparation_timeout','message':'终端工作副本准备超时，请重试'}) from error
    async def finalize(session):
        if not session.release_attempted:return  # Startup cleanup owns the unused copy.
        async def authorized(db):
            current = await _authorize_terminal(db,case_id,user_id)
            if current is None or current.s3_prefix!=source_prefix:
                raise CaseWriteConflict('input_stale','终端算例来源已变化，已保留本次副本')
            await terminal_records.current(db,session.owner)
            return current
        if session.exporter is not None:
            from ..services.sandbox_export_publication import publish
            session.export_receipt=await publish(session.exporter,session.id,case_id,source_prefix,authorized)
            if staging_prefix is not None:
                await finish_before_cancel(asyncio.to_thread(_cleanup_staging,staging_prefix,synchronized=True))
            return
        if staging_prefix is not None:
            from ..services.staging_sync import sync_authorized
            await sync_authorized(staging_prefix,source_prefix,case_id,authorized)
            await finish_before_cancel(asyncio.to_thread(_cleanup_staging,staging_prefix,synchronized=True))
    session=TerminalSession(case_id,user_id,source_prefix,staging_prefix)
    resources['session']=session
    session.on_closed=_terminal_closed
    await session.start(cwd=cwd,cols=cols,rows=rows,shell=shell,
        distro=get_runtime_setting('wsl_distro','vibeflow-linux'),reauthorize=reauthorize,finalize=finalize,
        credential=credential,environment=environment)
    _sessions[session.id]=session
    supervisor_register('terminal',session.id,case_id,lambda:_stop_terminal(session))
    if session.cleanup_verified is True and session.persisted_terminal:_terminal_closed(session)
    return session


def _terminal_closed(session):
    supervisor_unregister('terminal:'+session.id)
    if session.resource_limit_reason is not None and _sessions.get(session.id) is session:
        _sessions.pop(session.id)


async def _stop_terminal(session,reason='cancelled',actor_user_id=None):
    if isinstance(session,ClosedTerminal):
        _sessions.pop(session.id,None);return
    try:
        if actor_user_id is not None:
            from ..services import audit
            await audit.record_request(session.case_id, actor_user_id, 'terminal.close.requested',
                                       'terminal', session.id)
        await session.close(reason)
    finally:
        if session.cleanup_verified is True and session.persisted_terminal and not session.error:
            _sessions.pop(session.id,None);supervisor_unregister('terminal:'+session.id)


async def _session_row(sid,user_id):
    async with SessionLocal() as db:
        row=await db.get(NativeSession,sid)
        if row is None or row.created_by!=user_id:return None
        await _authorize_terminal(db,row.case_id,user_id)
        return row


def _close_progress(row):
    local=_sessions.get(row.id)
    closing=isinstance(local,TerminalSession) and local._close_task is not None and not local._close_task.done()
    retained=terminal_completion.available(row.id)
    pending=isinstance(local,TerminalSession) and local._pending_completion is not None and not local.persisted_terminal
    return {'finalizing':closing,'completion_pending':pending or retained,
        'completion_retained':retained,'close_error':local.error if local is not None else None,
        # Advisory observations remain separate from the authoritative SQL
        # cleanup_verified field. A retained file alone proves no native stop.
        'actual_stop_observed':local.cleanup_verified if local is not None else None,
        'owner_available':local is not None or terminal_routing.endpoint(row) is not None}


def _request_close(session,reason='cancelled',actor_user_id=None):
    task=_close_requests.get(session.id)
    if task is None or task.done():
        task=asyncio.create_task(_stop_terminal(session,reason,actor_user_id))
        _close_requests[session.id]=task
        def finished(done):
            if not done.cancelled():done.exception()
            if _close_requests.get(session.id) is done:_close_requests.pop(session.id,None)
        task.add_done_callback(finished)
    return task


async def close_all_terminal_sessions():
    """Drain detached sessions before API exit, retaining each original close."""
    global _reaper_task,_reaper_started
    reaper=_reaper_task;_reaper_task=None;_reaper_started=False
    if reaper is not None:
        reaper.cancel();await asyncio.gather(reaper,return_exceptions=True)
    pending=set(_close_requests.values())
    pending.update(_request_close(session,reason='shutdown') for session in list(_sessions.values()))
    if pending:
        results=await finish_before_cancel(asyncio.gather(*pending,return_exceptions=True))
        failures=[value for value in results if isinstance(value,BaseException)]
        if failures:raise RuntimeError('Terminal shutdown incomplete: '+', '.join(type(error).__name__ for error in failures))


async def _open_terminal_connection(ws, case, uid, cols, rows, shell, provider_profile_id, cli_harness):
    pending=deque();opened=None;published=False

    async def open_session():
        nonlocal opened,published
        opened=await _new_session(case,uid,cols,rows,shell,provider_profile_id,cli_harness)
        await ws.send_json({'type':'ready','session':opened.id,'alive':opened.alive})
        published=True

    async def watch_connection():
        buffered=0
        while True:
            message=await ws.receive()
            if message['type']=='websocket.disconnect':
                raise WebSocketDisconnect(message.get('code',1000))
            data=message.get('bytes')
            if data is None:
                text=message.get('text') or ''
                data=text.encode('utf8') if len(text)<=1024*1024 else text
            buffered+=len(data)
            if len(pending)>=256 or buffered>1024*1024:
                raise HTTPException(429,{'code':'terminal_startup_input_limit','message':'终端就绪前的输入过多，请重试'})
            pending.append(message)

    startup=asyncio.create_task(open_session())
    socket=asyncio.create_task(watch_connection())
    try:
        done,_=await asyncio.wait((startup,socket),return_when=asyncio.FIRST_COMPLETED)
        if socket in done:socket.result()
        startup.result()
        return opened,pending
    finally:
        async def finish():
            for task in (startup,socket):
                if not task.done():task.cancel()
            await asyncio.gather(startup,socket,return_exceptions=True)
            if opened is not None and not published:await _stop_terminal(opened)
        await finish_before_cancel(finish())


@router.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket, cols: int = 80, rows: int = 24,
                      shell: str = "cmd", session: str = "", case: int = 0,
                      token: str = "", provider_profile_id: int | None = None, cli_harness: str = ""):
    global _reaper_started,_reaper_task
    peer_ticket = ws.headers.get(terminal_routing.TICKET_HEADER)
    if peer_ticket and not session:
        await ws.close(code=4401)
        return
    uid = await connection_user_id(ws, peer_ticket or token,
                                   scope='terminal.reconnect' if peer_ticket else 'terminal',
                                   target=terminal_target(case, session))
    if uid is None:
        await ws.close(code=4401)
        return
    if not _reaper_started:
        _reaper_started = True
        _reaper_task=asyncio.get_running_loop().create_task(_reaper())
    await ws.accept()
    early_messages=deque()
    try:
        if shell not in {"cmd", "wsl", "wsl-sandbox"} or not (1 <= cols <= 1000 and 1 <= rows <= 1000):
            raise HTTPException(400, "终端shell或行列参数无效")
        if session:
            if provider_profile_id is not None or cli_harness:raise HTTPException(400,'挂接现有终端不能更换 API 配置，请新建会话')
            sess = _sessions.get(session)
            if sess is None:
                row=await _session_row(session,uid)
                if row is not None:
                    state=terminal_records.public(row)
                    if state['phase'] in {'closed','failed'}:sess=ClosedTerminal(row)
                    else:
                        origin = terminal_routing.endpoint(row) if not peer_ticket and state['phase'] == 'active' else None
                        if origin is not None:
                            if case and case != row.case_id:
                                raise HTTPException(403, '会话与请求算例不匹配')
                            await terminal_routing.reconnect(ws, origin, session, case, shell, uid)
                            return
                        await ws.send_json({'type':'error','code':state['phase'],'message':'终端所属实例当前不可连接，请查看会话记录'})
                        await ws.close(code=4409);return
            if sess is None or sess.user_id != uid:
                await ws.close(code=4403)
                return
            async with _terminal_access(sess.case_id, uid):
                if case and case != sess.case_id:
                    raise HTTPException(403, "会话与请求算例不匹配")
            await ws.send_json({'type':'ready','session':sess.id,'alive':sess.alive})
        else:
            sess,early_messages=await _open_terminal_connection(ws,case,uid,cols,rows,shell,provider_profile_id,cli_harness)
    except WebSocketDisconnect:
        return
    except (HTTPException, CaseWriteConflict) as error:
        capacity = _capacity_failure(error)
        detail=error.detail if isinstance(error,HTTPException) else error.detail()
        reason=detail.get('code') if isinstance(detail,dict) else None
        message=detail.get('message',str(detail)) if isinstance(detail,dict) else str(detail)
        if reason in {'terminal_disk_limit','terminal_disk_unverified'}:
            await ws.send_json({'type':'error','code':'terminal_resource_limit','reason':reason.removeprefix('terminal_'),
                                'message':message,'status':409})
            await ws.close(code=1013)
            return
        throttled=reason in {'provider_rate_limited','rate_limiter_unavailable'}
        specific=throttled or reason in {'input_stale','terminal_preparation_busy','terminal_preparation_timeout',
                                        'terminal_startup_input_limit','model_quota_exceeded'}
        await ws.send_json({"type": "error", "message": message,
                            "code": reason if specific else "storage_capacity" if capacity else "terminal_unavailable",
                            "status": error.status_code if isinstance(error, HTTPException) else 409,
                            **({'retry_after': detail['retry_after']} if throttled else {})})
        await ws.close(code=1013 if capacity or throttled or reason=='terminal_preparation_timeout'
                       else 4400 if isinstance(error, HTTPException) and error.status_code == 400 else 4403)
        return
    except Exception:
        await ws.send_json({"type": "error", "message": "终端启动失败，请检查运行环境", "code": "terminal_start_failed"})
        await ws.close(code=1011)
        return
    q, snap = sess.subscribe()

    async def send_output(operation):
        # Avoid a separate scheduled task per frame: a healthy viewer must be
        # able to drain queued frames while the PTY produces a burst.
        async with asyncio.timeout(SEND_TIMEOUT):await operation

    async def forward_loop():
        try:
            if snap:
                await send_output(ws.send_bytes(snap.encode("utf-8", "replace")))
            while True:
                item = await q.get()
                if q.overflow.is_set():return
                if item is None:
                    if sess.cleanup_verified is True and sess.persisted_terminal and not sess.error:
                        await send_output(ws.send_json({"type": "exit", "code": sess.exit_code}))
                    elif sess.resource_limit_reason is not None:
                        message=RESOURCE_LIMIT_MESSAGES[sess.resource_limit_reason]
                        await send_output(ws.send_json({'type':'error','code':'terminal_resource_limit',
                            'reason':sess.resource_limit_reason,'message':message}))
                    else:
                        await send_output(ws.send_json({"type": "error", "code":"terminal_cleanup_incomplete", "message":"终端收尾证据不完整，请查看会话记录"}))
                    break
                await send_output(ws.send_bytes(item.encode("utf-8", "replace")))
        except TimeoutError:
            q.fail()
        except Exception:
            pass

    async def receive_loop():
        async def input_data(data):
            async with _terminal_access(sess.case_id,uid):
                accepted=await sess.write(data)
            if accepted is False:
                await ws.send_json({'type':'error','code':'terminal_input_limit','message':'终端输入缓冲已满，请等待后继续'})
        async def handle_message(msg):
            if msg.get("bytes") is not None:
                await input_data(msg['bytes'].decode('utf-8','replace'))
            elif msg.get("text") is not None:
                try:
                    ctrl = json.loads(msg["text"])
                except json.JSONDecodeError:
                    return
                if not isinstance(ctrl, dict):
                    return
                t = ctrl.get("type")
                if t=='input' and isinstance(ctrl.get('data'),str):
                    await input_data(ctrl['data'])
                elif t in ('resize','ready'):
                    accepted=False
                    async with _terminal_access(sess.case_id,uid):
                        try:
                            width, height = int(ctrl.get("cols", 80)), int(ctrl.get("rows", 24))
                            if 1 <= width <= 1000 and 1 <= height <= 1000:
                                await sess.resize(width, height)
                                accepted=True
                        except (ValueError, TypeError):
                            return
                    if accepted:
                        await ws.send_json({'type':'resize_accepted','cols':width,'rows':height})
        try:
            while True:
                msg = early_messages.popleft() if early_messages else await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                try:
                    await handle_message(msg)
                except HTTPException as error:
                    if not _capacity_failure(error):raise
                    # A temporary storage refusal rejects this message. Keep
                    # the PTY and connection available for an explicit retry.
                    control=json.loads(msg['text']) if msg.get('text') is not None else {'type':'input'}
                    operation=control.get('type')
                    detail=error.detail if isinstance(error.detail,dict) else {}
                    await ws.send_json({'type':'error','code':'storage_capacity',
                        'operation':operation,'retryable':operation in ('resize','ready') and detail.get('code')=='storage_quota_busy',
                        'status':error.status_code,'message':detail.get('message',str(error.detail))})
        except WebSocketDisconnect:
            pass
        except (HTTPException, CaseWriteConflict):
            await ws.send_json({"type": "error", "code": "terminal_unavailable", "message": "终端权限或算例状态已变化"})
            try:
                await _stop_terminal(sess)
            finally:
                await ws.close(code=4403)

    tasks = [asyncio.create_task(forward_loop()), asyncio.create_task(receive_loop()),asyncio.create_task(q.overflow.wait())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        sess.unsubscribe(q)
        if q.overflow.is_set():
            try:
                await asyncio.wait_for(ws.send_json({'type':'error','code':'terminal_output_limit',
                    'message':'当前连接接收输出过慢，已断开；会话已保留，可重新连接查看最近输出，命令不会自动重发。'}),CLOSE_TIMEOUT)
            except Exception:pass
            try:await asyncio.wait_for(ws.close(code=1013),CLOSE_TIMEOUT)
            except Exception:pass
        # 注意：WS 断开不杀 PTY（多标签重连语义）；显式关闭走 DELETE


@router.get('/api/v1/terminal/sessions')
async def list_sessions(include_closed: bool=False,user: User=Depends(get_current_user)):
    async with SessionLocal() as db:
        rows=(await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id).order_by(NativeSession.created_at))).all()
        items=[]
        for row in rows:
            local=_sessions.get(row.id)
            if row.phase in {'closed','failed'} and local is None and not include_closed:continue
            value=terminal_records.public(row)
            items.append({'id':row.id,'shell':row.shell,'case_id':row.case_id,'alive':local.alive if local is not None else
                False if value['phase'] in {'closed','failed'} and value['cleanup_verified'] is True else None,'phase':value['phase'],'cleanup_verified':value['cleanup_verified'],
                'error':value['error'],
                **_close_progress(row),
                'pending_input_bytes':local.pending_input_bytes if isinstance(local,TerminalSession) else None})
    return {'sessions':items}


@router.get('/api/v1/terminal/sessions/{sid}')
async def get_session(sid: str,user: User=Depends(get_current_user)):
    row=await _session_row(sid,user.id)
    if row is None:raise HTTPException(404,'会话不存在')
    return {**terminal_records.public(row),**_close_progress(row)}


@router.get('/api/v1/terminal/sessions/{sid}/recovery-inspection')
async def inspect_recovery(sid: str, user: User=Depends(get_current_user)):
    from sqlalchemy.exc import SQLAlchemyError
    from ..services import sandbox_recovery_inspection as inspection
    from ..services.case_write import assert_case_available

    async def snapshot():
        async with SessionLocal() as db:
            row = await db.get(NativeSession, sid)
            if row is None or row.created_by != user.id:
                raise HTTPException(404, '会话不存在')
            case = await _authorize_terminal(db, row.case_id, user.id)
            if case is None:
                raise inspection.InspectionBlocked('persistent_sandbox_required')
            assert_case_available(case)
            return await inspection.capture(db, row, case.s3_prefix)

    try:
        before = await snapshot()
        observed = await inspection.observe(before)
        # Authorization and durable state are refreshed after slow OS reads.
        # This result remains advisory; a recovery executor must recheck it.
        after = await snapshot()
        if before.fingerprint != after.fingerprint:
            return inspection.result(sid, reason='records_changed_during_inspection')
        return observed
    except inspection.InspectionBlocked as error:
        return inspection.result(sid, reason=error.reason)
    except CaseWriteConflict:
        return inspection.result(sid, reason='case_unavailable')
    except (ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError):
        return inspection.result(sid, reason='ownership_records_unverified')


recovery_router=APIRouter(route_class=SandboxRecoveryDeadlineRoute)


@recovery_router.post('/api/v1/terminal/sessions/{sid}/recoveries', status_code=201)
async def recover_session(sid: str, user: User=Depends(get_current_user)):
    from ..services import sandbox_recovery_execution as recovery
    from ..services.sandbox_recovery_inspection import InspectionBlocked
    try:
        identifier = await recovery.recover(sid, user.id)
        value, _ = await recovery.load(sid, identifier, user.id)
        return value
    except InspectionBlocked as error:
        raise HTTPException(409, {'code': error.reason, 'message': '恢复前核验未通过，请查看会话记录'})
    except CaseWriteConflict as error:
        raise HTTPException(409, {'code': error.code, 'message': '当前算例或节点存储无法接受恢复操作'})
    except (ValueError, OSError, TimeoutError):
        raise HTTPException(502, {'code': 'sandbox_recovery_incomplete', 'message': '恢复未完成，已保留原镜像和操作证据'})


router.include_router(recovery_router)


@router.get('/api/v1/terminal/sessions/{sid}/recoveries/{identifier}')
async def get_recovery(sid: str, identifier: str, user: User=Depends(get_current_user)):
    from ..services import sandbox_recovery_execution as recovery
    try:
        value, _ = await recovery.load(sid, identifier, user.id)
        return value
    except CaseWriteConflict:
        raise HTTPException(409, {'code': 'case_unavailable'})
    except (ValueError, OSError):
        raise HTTPException(409, {'code': 'recovery_evidence_invalid'})


class SandboxRetirementConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recovery_id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    confirm: Literal[True]


@router.get('/api/v1/terminal/sessions/{sid}/retirement')
async def sandbox_retirement_status(sid: str, user: User=Depends(get_current_user)):
    from ..services.sandbox_retirement import status
    try:
        return await status(sid,user.id)
    except CaseWriteConflict as error:
        raise HTTPException(409,error.detail())
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409,{'code':'retirement_evidence_invalid'})


@router.post('/api/v1/terminal/sessions/{sid}/retirement')
async def retire_sandbox(sid: str, body: SandboxRetirementConfirmation, user: User=Depends(get_current_user)):
    from ..services.sandbox_retirement import retire
    from ..services.sandbox_recovery_inspection import InspectionBlocked
    try:
        return await retire(sid,body.recovery_id,user.id)
    except InspectionBlocked as error:
        raise HTTPException(409,{'code':error.reason})
    except CaseWriteConflict as error:
        raise HTTPException(409,error.detail())
    except (OSError, ValueError, KeyError, TypeError, AttributeError, TimeoutError):
        raise HTTPException(409,{'code':'retirement_evidence_invalid'})


@router.get('/api/v1/terminal/sessions/{sid}/staging-cleanup')
async def sandbox_staging_cleanup_status(sid: str, user: User=Depends(get_current_user)):
    from ..services.sandbox_staging_cleanup import status
    try:
        return await status(sid, user.id)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(409, {'code': 'staging_cleanup_evidence_invalid'})


staging_cleanup_router=APIRouter(route_class=StagingCleanupDeadlineRoute)


@staging_cleanup_router.post('/api/v1/terminal/sessions/{sid}/staging-cleanup')
async def clean_sandbox_staging(sid: str, body: SandboxRetirementConfirmation, user: User=Depends(get_current_user)):
    from ..services.sandbox_staging_cleanup import clean
    try:
        return await clean(sid, body.recovery_id, user.id)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail())
    except (OSError, ValueError, KeyError, TypeError, AttributeError, TimeoutError):
        raise HTTPException(409, {'code': 'staging_cleanup_incomplete',
            'message': '暂存清理未完成，请保留当前记录并重试；未知或变化的内容不会被删除'})


router.include_router(staging_cleanup_router)


@router.get('/api/v1/terminal/sessions/{sid}/image-cleanup')
async def sandbox_image_cleanup_status(sid: str, user: User=Depends(get_current_user)):
    from ..services.sandbox_image_cleanup import status
    try:
        return await status(sid,user.id)
    except CaseWriteConflict as error:
        raise HTTPException(409,error.detail())
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        raise HTTPException(409,{'code':'image_cleanup_evidence_invalid'})


image_cleanup_router=APIRouter(route_class=ImageCleanupDeadlineRoute)


@image_cleanup_router.post('/api/v1/terminal/sessions/{sid}/image-cleanup')
async def clean_sandbox_image(sid: str, body: SandboxRetirementConfirmation, user: User=Depends(get_current_user)):
    from ..services.sandbox_image_cleanup import clean
    from ..services.native_execution_client import ChildExecutionError
    try:
        return await clean(sid,body.recovery_id,user.id)
    except CaseWriteConflict as error:
        raise HTTPException(409,error.detail())
    except (OSError,ValueError,KeyError,TypeError,AttributeError,TimeoutError,ChildExecutionError):
        raise HTTPException(409,{'code':'image_cleanup_incomplete',
            'message':'镜像清理未完成，请查询登记记录；恢复快照和原始元数据保留'})


router.include_router(image_cleanup_router)


class RecoveryFileSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(min_length=1, max_length=10000)
    semantics: Literal['file_contents_only', 'sandbox_file_changes', 'case_dictionary', 'sandbox_tree_changes'] = 'file_contents_only'


class RecoveryFileConfirmation(RecoveryFileSelection):
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    request_id: str = Field(min_length=36, max_length=36)
    confirm: Literal[True]
    semantics: Literal['file_contents_only', 'sandbox_file_changes', 'case_dictionary', 'sandbox_tree_changes']


@router.get('/api/v1/terminal/sessions/{sid}/recoveries/{identifier}/tree-changes')
async def recovery_tree_changes(sid: str, identifier: str, user: User=Depends(get_current_user)):
    from ..services.sandbox_tree_recovery import changes
    try:
        return await changes(sid, identifier, user.id)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail())
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        raise HTTPException(409, {'code':'recovery_evidence_invalid'})


@router.get('/api/v1/terminal/sessions/{sid}/recoveries/{identifier}/changes')
async def recovery_changes(sid: str, identifier: str, user: User=Depends(get_current_user)):
    from ..services.sandbox_recovery_restore import changes
    try:
        return await changes(sid, identifier, user.id)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail())
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        raise HTTPException(409, {'code':'recovery_evidence_invalid'})


@router.post('/api/v1/terminal/sessions/{sid}/recoveries/{identifier}/restore-plan')
async def preview_recovery_files(sid: str, identifier: str, body: RecoveryFileSelection,
                                 user: User=Depends(get_current_user)):
    from ..services.sandbox_recovery_restore import preview
    try:
        return await preview(sid, identifier, user.id, body.paths, body.semantics)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail())
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        raise HTTPException(409, {'code':'recovery_restore_unavailable', 'message':'无法完整核验选中文件，请检查快照及当前项目后重试'})


@router.post('/api/v1/terminal/sessions/{sid}/recoveries/{identifier}/restore')
async def restore_recovery_files(sid: str, identifier: str, body: RecoveryFileConfirmation,
                                user: User=Depends(get_current_user)):
    from ..services.sandbox_recovery_restore import restore
    try:
        return await restore(sid, identifier, user.id, body.paths, body.plan_sha256, body.request_id, body.semantics)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail())
    except (OSError, ValueError, KeyError, TypeError, TimeoutError):
        raise HTTPException(409, {'code':'recovery_restore_unavailable', 'message':'写回未获确认，请查询操作记录并重新核验，勿重复创建确认标识'})


@router.get('/api/v1/terminal/sessions/{sid}/recoveries/{identifier}/restores/{request_id}')
async def recovery_restore_status(sid: str, identifier: str, request_id: str,
                                  user: User=Depends(get_current_user)):
    from ..services.sandbox_recovery_restore import status
    try:
        return await status(sid, identifier, user.id, request_id)
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail())
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(409, {'code':'recovery_evidence_invalid'})


@router.get('/api/v1/terminal/sessions/{sid}/recoveries')
async def list_recoveries(sid: str, before: str | None=None, user: User=Depends(get_current_user)):
    from ..services import sandbox_recovery_execution as recovery
    try:
        return await recovery.list_operations(sid, user.id, before)
    except CaseWriteConflict:
        raise HTTPException(409, {'code': 'case_unavailable'})


@router.get('/api/v1/terminal/sessions/{sid}/recoveries/{identifier}/download')
async def download_recovery(sid: str, identifier: str, request: Request, user: User=Depends(get_current_user)):
    from fastapi.responses import StreamingResponse
    from ..archive_admission import RECOVERY_DOWNLOAD_STREAM
    from ..services import request_deadline, sandbox_recovery_execution as recovery

    try:
        value, selected = await recovery.load(sid, identifier, user.id)
        if value['phase'] != 'succeeded':
            raise HTTPException(409, {'code': 'recovery_not_complete'})

        async def prepare():
            stream = await asyncio.to_thread(recovery.download, selected, value['receipt'])
            # The admission middleware closes this handle even when the
            # request was cancelled while the opening worker was draining.
            request.scope['state'][RECOVERY_DOWNLOAD_STREAM] = stream
            return stream

        stream = await finish_before_cancel(prepare())
        # Reauthorize after potentially slow archive hashing.
        refreshed, refreshed_selection = await recovery.load(sid, identifier, user.id)
        if refreshed.get('receipt') != value['receipt'] or refreshed_selection != selected:
            raise HTTPException(409, {'code': 'recovery_evidence_changed'})
        size = value['receipt']['archive_bytes']

        async def chunks():
            deadline = request_deadline.current()

            def read_block(length):
                deadline.check()
                return stream.read(length)

            remaining = size
            while remaining:
                deadline.check()
                block = await finish_before_cancel(asyncio.to_thread(read_block, min(1024 * 1024, remaining)))
                deadline.check()
                if not block:
                    raise OSError('Recovery archive shortened during download')
                remaining -= len(block)
                yield block

        return StreamingResponse(chunks(), media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename="sandbox-recovery-' + identifier + '.zip"',
                     'Content-Length': str(size), 'Cache-Control': 'private, no-store',
                     'X-Content-Type-Options': 'nosniff'})
    except TimeoutError:
        raise
    except (ValueError, OSError, CaseWriteConflict) as error:
        raise HTTPException(409, {'code': 'recovery_evidence_invalid'}) from error


@router.delete('/api/v1/terminal/sessions/{sid}')
async def close_session(sid: str, request: Request, wait: bool = True,
                        credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    peer_ticket = request.headers.get(terminal_routing.TICKET_HEADER)
    if peer_ticket:
        user_id = await connection_user_id(request, peer_ticket, scope='terminal.close', target=terminal_target(0, sid))
        if user_id is None:
            raise HTTPException(401, '终端关闭票据无效')
    else:
        async with SessionLocal() as db:
            user_id = (await get_current_user(request, credentials, db)).id
    row=await _session_row(sid,user_id)
    if row is None:return JSONResponse({'ok':False,'message':'会话不存在'},status_code=404)
    session=_sessions.get(sid)
    if session is None:
        if terminal_completion.available(sid):
            try:
                await terminal_completion.recover(row)
            except terminal_completion.CompletionBlocked as error:
                raise HTTPException(409,{'code':error.code,'message':'保留的终端关闭结果当前无法恢复'})
            except Exception:
                raise HTTPException(502,{'code':'terminal_cleanup_incomplete','message':'关闭结果尚未持久完成，可重试原结果提交'})
            row=await _session_row(sid,user_id)
            if row is None:raise HTTPException(404,'会话不存在')
        value=terminal_records.public(row)
        if value['phase']=='closed' and value['cleanup_verified'] is True:return {'ok':True}
        origin = terminal_routing.endpoint(row) if not peer_ticket and value['phase'] in {'starting', 'active'} else None
        if origin is not None:
            return await terminal_routing.close(origin, sid, user_id, wait)
        raise HTTPException(409,{'code':value['phase'],'message':'终端所属实例当前不可控制，请查看会话记录'})
    task=_request_close(session,actor_user_id=user_id)
    if not wait:
        return JSONResponse({'ok':False,'accepted':True,'id':sid,'status':'closing'},status_code=202)
    try:await finish_before_cancel(task)
    except Exception:raise HTTPException(502,{'code':'terminal_cleanup_incomplete','message':'终端收尾尚未核验完成'})
    return {'ok':True}
