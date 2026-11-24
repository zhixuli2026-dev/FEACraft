"""Durable owned rendering sessions, independent of engineering solver Jobs."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path,PurePosixPath,PureWindowsPath
import socket
import sys
from threading import BoundedSemaphore
import time
from typing import Literal

import httpx
from fastapi import APIRouter,Depends,HTTPException,Request,Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import SessionLocal,get_db
from ..deps import get_current_user
from ..models import NativeVizSession,User
from ..services.async_ops import finish_before_cancel
from ..services.case_write import CaseWriteConflict
from ..services.case_write_capacity import committing
from ..services.event_replay import latest_sequence
from ..services.quota import check_viz_quota
from ..services.request_deadline import scope as deadline_scope
from ..services.request_lifecycle import wait as wait_request
from ..services.result_readers import SUPPORTED_RESULT_EXTENSIONS
from ..services.supervisor import register as supervisor_register,unregister as supervisor_unregister
from ..services import viz_records as records
from ..services.viz_routing import endpoint as owner_endpoint
from ..services.viz_sessions import VizSession
from ..services.viz_ports import reserve as reserve_port
from ..storage import _fs_path,_use_s3,prepare_job_staging
from .terminal import _cleanup_staging
from .workspaces import get_owned_case

router=APIRouter(prefix='/api/v1',tags=['viz'])
MAX_SESSIONS_PER_CASE=2
VIZ_IDLE_SECONDS=float(os.getenv('VIBEFLOW_VIZ_IDLE_SECONDS','1800'))
VIZ_IDLE_CHECK_INTERVAL=float(os.getenv('VIBEFLOW_VIZ_IDLE_CHECK_INTERVAL','60'))
_sessions: dict[int,VizSession]={}
_admission_lock=asyncio.Lock()
_staging_slots=BoundedSemaphore(2)


def _forget_session(session):
    _sessions.pop(session.id,None);supervisor_unregister(f'viz:{session.id}')


def _remember_session(session):
    _sessions[session.id]=session
    supervisor_register('viz',session.id,session.case_id,lambda:session.close())


async def _close_session_async(sid,session):return await session.close()


async def _reap_idle_sessions(max_age=None):
    cutoff=time.time()-(VIZ_IDLE_SECONDS if max_age is None else max_age)
    for session in tuple(_sessions.values()):
        if session._close_task is not None:continue
        if (session.alive is True and session.last_activity<cutoff) or (session.owned.proc is not None and session.owned.proc.returncode is not None):
            try:await session.close('root_exit' if session.owned.proc is not None and session.owned.proc.returncode is not None else 'cancelled')
            except Exception as error:print('[WARN] viz close failed: '+type(error).__name__)


def start_viz_reaper():
    from .viz_collaboration import discard_expired

    async def loop():
        while True:
            await asyncio.sleep(VIZ_IDLE_CHECK_INTERVAL);await _reap_idle_sessions()
            try:await discard_expired()
            except Exception as error:print('[WARN] viz collaboration cleanup failed: '+type(error).__name__)
    return asyncio.create_task(loop())


async def close_all_viz_sessions():
    results=await asyncio.gather(*(session.close() for session in tuple(_sessions.values())),return_exceptions=True)
    if any(isinstance(value,BaseException) for value in results):raise RuntimeError('Some rendering sessions have incomplete cleanup')


def _free_port(start=8500):
    reserved={s.port for s in tuple(_sessions.values())}
    for port in range(start,65536):
        if port in reserved:continue
        reservation=reserve_port(port)
        if reservation is None:continue
        try:
            with socket.socket() as probe:
                if os.name=='nt':probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                probe.bind(('127.0.0.1',port))
        except OSError:reservation.close();continue
        except BaseException:reservation.close();raise
        return reservation
    raise HTTPException(503,'无可用端口')


def _safe_relative(value):
    text=value.replace('\\','/');path=PurePosixPath(text)
    if not text or '\0' in text or path.is_absolute() or PureWindowsPath(text).drive or '..' in path.parts:
        raise HTTPException(400,'非法相对路径')
    return path.as_posix()


def _fingerprint(path):
    stamp=path.stat()
    if not path.is_file():raise HTTPException(404,'可视化输入文件不存在')
    return {'size':stamp.st_size,'mtime_ns':stamp.st_mtime_ns}


def _select_input(source_prefix,stage,body):
    root=_fs_path(stage or source_prefix,'.').resolve();file_path=None
    if body.file_path:
        file_path=_safe_relative(body.file_path);run_dir=PurePosixPath(file_path).parent.as_posix()
        if PurePosixPath(file_path).suffix.lower() not in SUPPORTED_RESULT_EXTENSIONS:raise HTTPException(400,'不支持的结果文件扩展名')
        target=(root/file_path).resolve()
    else:
        run_dir=_safe_relative(body.run_dir);folder=(root/run_dir).resolve()
        if folder != root and root not in folder.parents:raise HTTPException(400,'求解目录越过 case 根目录')
        if not (folder/'system'/'controlDict').is_file():raise HTTPException(400,f'{run_dir} 不是有效的 OpenFOAM 算例目录（缺 system/controlDict）')
        marker_name=PurePosixPath(source_prefix).name if run_dir=='.' else folder.name
        target=(folder/(marker_name+'.foam')).resolve()
        if run_dir=='.' and not target.exists():
            target=next((entry.resolve() for entry in folder.iterdir()
                         if entry.suffix.lower()=='.foam' and entry.is_file() and entry.resolve().parent==folder),target)
        if target.parent!=folder:raise HTTPException(400,'OpenFOAM 标记文件越过求解目录')
        if not target.exists():target.touch()
    if root not in target.parents:raise HTTPException(400,'结果文件越过 case 根目录')
    if not target.is_file():raise HTTPException(404,'可视化输入文件不存在')
    folder=Path(__file__).resolve().parents[1]/'services'
    executable=str(Path(sys.executable).resolve())
    code_names=records.CODE_NAMES
    engine={}
    if body.engine=='paraview':
        from ..services.viz_paraview_kernel import INPUT_TYPES
        if target.suffix.casefold() not in INPUT_TYPES:
            raise HTTPException(400,'原生 ParaView 视图需要适用的 VTK 或 OpenFOAM 数据')
        executable=_paraview_visualizer()
        code_names=records.PARAVIEW_CODE_NAMES
        engine={'engine':'paraview'}
    return {'nonce':'0'*64,'python':executable,'case_root':str(root),
        'source_prefix':source_prefix,'staging_prefix':stage,'run_dir':run_dir,'file_path':file_path,
        'input_path':str(target),'input_fingerprint':_fingerprint(target),'port':0,
        'code':{name:hashlib.sha256((folder/(name+'.py')).read_bytes()).hexdigest() for name in code_names},**engine}


def _paraview_visualizer():
    from ..services.paraview import paraview_executable,visualizer_path
    try:
        executable=paraview_executable('pvpython')
        visualizer_path(executable)
        return str(Path(executable).resolve())
    except (OSError,ValueError) as error:
        raise HTTPException(503,'当前运行环境未安装 ParaView Web Visualizer') from error


async def _authorize(db,case_id,user_id):
    user=await db.get(User,user_id)
    if user is None or user.status!='active':raise HTTPException(403,'可视化用户已不可用')
    return await get_owned_case(case_id,user,db)


async def _reauthorize(session):
    async with SessionLocal() as db:
        case=await _authorize(db,session.case_id,session.user_id)
        async with committing(db,case,wait_for_quota=True):
            if case.s3_prefix!=session.selected['source_prefix']:raise CaseWriteConflict('input_stale','可视化算例来源已变化')
            def inspect():
                chosen=session.selected;root=_fs_path(chosen['staging_prefix'] or case.s3_prefix,'.').resolve()
                target=Path(chosen['input_path'])
                if str(root)!=chosen['case_root'] or target.resolve()!=target or root not in target.parents or _fingerprint(target)!=chosen['input_fingerprint']:
                    raise CaseWriteConflict('input_stale','可视化输入已变化')
            await finish_before_cancel(asyncio.to_thread(inspect))


async def _finalize(session):
    stage=session.selected['staging_prefix']
    if stage is None:return
    if session.ready_birth is None:
        await finish_before_cancel(asyncio.to_thread(_cleanup_staging,stage));return
    async def authorized(db):
        case=await _authorize(db,session.case_id,session.user_id)
        await records.current(db,session.owner)
        return case
    from ..services.staging_sync import sync_authorized
    from ..services.viz_scene_relay import scene_sync_excluded
    excluded = await scene_sync_excluded(session)
    await sync_authorized(stage,session.selected['source_prefix'],session.case_id,authorized,excluded=excluded)
    await finish_before_cancel(asyncio.to_thread(_cleanup_staging,stage,synchronized=True))


class VizSessionIn(BaseModel):
    run_dir: str='C-run/case-1'
    file_path: str | None=None
    engine: Literal['trame','paraview']='trame'


async def _check_viz_capacity(db, case, user_id):
    user_slots,case_slots=await records.occupied_slots(db,user_id,case_id=case.id)
    if case_slots>=MAX_SESSIONS_PER_CASE:
        raise HTTPException(409,'该算例已有 2 个活跃可视化会话（内存保护上限）')
    await check_viz_quota(db,await db.get(User,user_id),user_slots)


async def _staging_source(db, case):
    return case.s3_prefix,case.dict_revision,await latest_sequence(db,case.id)


async def _has_durable_owner(session):
    if session is None or session.owner is None:return False
    # Allocation sets the in-memory owner before the outer commit. On failure
    # a fresh read distinguishes a committed row from a rolled-back insertion.
    async with SessionLocal() as db:
        nonce=await db.scalar(select(NativeVizSession.launch_nonce).where(NativeVizSession.id==session.id))
    return nonce==session.owned.nonce


async def _create(case_id,body,user_id):
    resources={};session=None;registered=False
    staging=_use_s3()
    if staging and not _staging_slots.acquire(blocking=False):
        raise HTTPException(429,{'code':'viz_preparation_busy','message':'可视化源正在准备，请稍后重试'},
                            headers={'Retry-After':'1'})
    try:
        try:
            async with deadline_scope(get_settings().viz_prepare_timeout_seconds) as deadline:
                if staging:
                    async with SessionLocal() as db:
                        case=await _authorize(db,case_id,user_id)
                        async with committing(db,case,wait_for_quota=True):
                            await _check_viz_capacity(db,case,user_id)
                            source=await _staging_source(db,case)
                    def prepare():
                        stage=prepare_job_staging(source[0],time.time_ns())
                        resources['stage']=stage
                        deadline.check()
                        return _select_input(source[0],stage,body)
                    selected=await finish_before_cancel(asyncio.to_thread(prepare))
                async with _admission_lock:
                    async with SessionLocal() as db:
                        case=await _authorize(db,case_id,user_id)
                        async with committing(db,case,wait_for_quota=True):
                            if staging and source!=await _staging_source(db,case):
                                raise CaseWriteConflict('input_stale','可视化准备期间算例已变化，请重新打开')
                            # Serialize the authoritative quota check and insert across API workers.
                            await db.scalar(select(User.id).where(User.id==user_id).with_for_update())
                            await _check_viz_capacity(db,case,user_id)
                            if not staging:
                                selected=await finish_before_cancel(asyncio.to_thread(_select_input,case.s3_prefix,None,body))
                            deadline.check()
                            reservation=_free_port();resources['reservation']=reservation;selected['port']=reservation.port
                            session=VizSession(case_id,user_id,selected);session.port_reservation=reservation;session.finalize=_finalize
                            session.on_closed=_forget_session
                            await session.allocate(db=db)
                            deadline.check()
                    registered=True
                    _remember_session(session)
        except TimeoutError as error:
            raise HTTPException(504,{'code':'viz_preparation_timeout','message':'可视化准备超时，请稍后重试'}) from error
        await session.start(lambda:_reauthorize(session),_finalize)
        if staging and session.selected.get('engine')!='paraview':
            async def authorize_scene(db):
                return await _authorize(db, case_id, user_id)
            session.follow_committed_scenes(authorize_scene, after_sequence=source[2])
        from ..services.eventbus import publish
        publish(user_id,{'type':'scene.available','session_id':session.id,'case_id':case_id,'status':'created'})
        return session
    except BaseException as error:
        async def cleanup():
            if registered or await _has_durable_owner(session):
                await session.close('executor_error',error=type(error).__name__)
            else:
                try:
                    if resources.get('stage') is not None:
                        await asyncio.to_thread(_cleanup_staging,resources['stage'])
                finally:
                    if resources.get('reservation') is not None:resources['reservation'].close()
        try:await finish_before_cancel(cleanup())
        except BaseException as cleanup_error:error.add_note('Visualization cleanup: '+type(cleanup_error).__name__)
        raise
    finally:
        if staging:_staging_slots.release()


async def _owned_row(db,sid,user_id,*,case_access=True):
    row=await db.get(NativeVizSession,sid)
    if row is None or row.created_by!=user_id:raise HTTPException(404,'可视化会话不存在')
    if case_access:await _authorize(db,row.case_id,user_id)
    return row


async def _row(sid,user_id,*,case_access=True):
    async with SessionLocal() as db:
        return await _owned_row(db,sid,user_id,case_access=case_access)


def _out(row):
    value=records.public(row);session=_sessions.get(row.id)
    remote=(session is None and value['phase']=='active' and owner_endpoint(row) is not None)
    value.update(alive=session.alive if session is not None else True if remote else False if value['cleanup_verified'] is True else None,
        owner_available=session is not None or remote,finalizing=session is not None and session._close_task is not None and not session._close_task.done(),
        url=f"http://127.0.0.1:{value['port']}/index.html" if value['port'] is not None else None)
    if session is not None:value['last_activity']=session.last_activity
    return value


@router.post('/cases/{case_id}/viz:sessions')
async def create_viz_session(case_id:int,body:VizSessionIn,request:Request,user:User=Depends(get_current_user)):
    session=await wait_request(_create(case_id,body,user.id),request=request)
    return _out(await _row(session.id,user.id))


@router.post('/viz:sessions/{sid}:reopen')
async def reopen_viz_session(sid:int,request:Request,user:User=Depends(get_current_user)):
    row=await _row(sid,user.id)
    value=records.public(row)
    if value['phase'] not in ('closed','failed') or value['cleanup_verified'] is not True:
        raise HTTPException(409,{'code':'viz_reopen_requires_stop','message':'原可视化会话须先确认退出'})
    body=VizSessionIn(run_dir=value['run_dir'],file_path=value['file_path'],engine=value['engine'])
    session=await wait_request(_create(row.case_id,body,user.id),request=request)
    return _out(await _row(session.id,user.id))


@router.get('/cases/{case_id}/viz:engines')
async def viz_engines(case_id:int,user:User=Depends(get_current_user)):
    async with SessionLocal() as db:
        await _authorize(db,case_id,user.id)
    availability={'available':['trame'],'python_console':os.name=='nt'}
    try:
        _paraview_visualizer()
    except HTTPException:
        return availability
    availability['available'].append('paraview')
    return availability


@router.get('/cases/{case_id}/viz:sessions')
async def list_viz_sessions(case_id:int,include_closed:bool=False,user:User=Depends(get_current_user),
                            db:AsyncSession=Depends(get_db)):
    await _authorize(db,case_id,user.id)
    rows=(await db.scalars(select(NativeVizSession).where(NativeVizSession.case_id==case_id,
        NativeVizSession.created_by==user.id).order_by(NativeVizSession.created_at))).all()
    result=[]
    for row in rows:
        value=_out(row)
        if (include_closed or value['phase'] in {'starting','active','recovery_required','evidence_invalid'} or row.id in _sessions
                or (value['process'] is not None and value['cleanup_verified'] is not True)):result.append(value)
    return result


@router.get('/viz:sessions/{sid}')
async def get_viz_session(sid:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    return _out(await _owned_row(db,sid,user.id))


@router.delete('/viz:sessions/{sid}')
async def kill_viz_session(sid:int,request:Request,user:User=Depends(get_current_user)):
    row=await _row(sid,user.id,case_access=False);session=_sessions.get(sid)
    if session is None:
        value=records.public(row)
        if value['phase']=='closed' and value['cleanup_verified'] is True:return {'ok':True}
        remote=owner_endpoint(row)
        if remote is not None and value['phase'] in ('starting','active'):
            headers={key:request.headers[key] for key in ('authorization','origin','cookie') if key in request.headers}
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30,connect=4),trust_env=False) as client:
                    reply=await client.delete(f'{remote}/api/v1/viz:sessions/{sid}',headers=headers)
            except httpx.RequestError as error:
                raise HTTPException(503,{'code':'viz_owner_unavailable','message':'暂时无法连接可视化所属实例'}) from error
            return Response(reply.content,status_code=reply.status_code,media_type='application/json')
        raise HTTPException(409,{'code':value['phase'],'message':'可视化所属实例当前不可控制，请查看会话记录'})
    from ..services import audit
    await audit.record_request(row.case_id, user.id, 'viz.close.requested', 'viz_session', sid)
    try:await session.close()
    except Exception:raise HTTPException(502,{'code':'viz_cleanup_incomplete','message':'可视化收尾尚未核验完成'})
    from ..services.eventbus import publish
    publish(user.id,{'type':'scene.available','session_id':sid,'case_id':row.case_id,'status':'closed'})
    return {'ok':True}
