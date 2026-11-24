"""Case-scoped presenter and read-only viewport sharing."""

from datetime import datetime, timezone
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal, get_db
from ..deps import get_current_user
from ..models import NativeVizSession, User, VizPresentation, VizViewer
from ..services import viz_records
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1/cases/{case_id}/viz:collaboration', tags=['viz'])
PRESENTER_SECONDS = 20
VIEWER_SECONDS = 12
FRAME_LIMIT = 320 * 1024
FRAME_HEADERS = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'}


class PresentationIn(BaseModel):
    session_id: int = Field(gt=0)


def insert(db, model):
    return (postgres_insert if db.get_bind().dialect.name == 'postgresql' else sqlite_insert)(model)


def live(at):
    return (VizPresentation.expires_at > at, exists(select(NativeVizSession.id).where(
        NativeVizSession.id == VizPresentation.session_id,
        NativeVizSession.phase == 'active',
        exists(select(User.id).where(User.id==NativeVizSession.created_by,User.status=='active')),
        NativeVizSession.lease_expires_at > datetime.fromtimestamp(at,tz=timezone.utc))))


def stale():
    return HTTPException(409, {'code':'viz_presentation_changed','message':'共享已更换或停止，请重新连接'})


@router.get('')
async def snapshot(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    at = time.time()
    result = (await db.execute(select(VizPresentation, User.name).join(User, User.id==VizPresentation.presenter_id)
                               .where(VizPresentation.case_id==case_id, User.status=='active', *live(at)))).one_or_none()
    if result is None:
        return {'active':False}
    share, name = result
    viewers = (await db.execute(select(User.id,User.name).join(VizViewer,VizViewer.user_id==User.id)
                  .where(VizViewer.case_id==case_id,VizViewer.version==share.version,VizViewer.expires_at>at,
                         User.status=='active',User.id!=share.presenter_id).order_by(User.name, User.id))).all()
    return {'active':True,'presenter':{'id':share.presenter_id,'name':name},'session_id':share.session_id,
            'version':share.version,'sequence':share.sequence,
            'viewers':[{'id':viewer.id,'name':viewer.name} for viewer in viewers]}


@router.post(':start')
async def start(case_id: int, body: PresentationIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id,user,db,write=True)
    session = await db.get(NativeVizSession,body.session_id)
    if session is None or session.created_by!=user.id or session.case_id!=case_id:
        raise HTTPException(404,'可视化会话不存在')
    if viz_records.public(session)['phase']!='active':
        raise HTTPException(409,'请打开运行中的可视化会话')
    at, version = time.time(), uuid.uuid4().hex
    values = {'presenter_id':user.id,'session_id':session.id,'version':version,
              'expires_at':at+PRESENTER_SECONDS,'sequence':0,'frame':None}
    claim = insert(db,VizPresentation).values(case_id=case_id,**values).on_conflict_do_update(
        index_elements=[VizPresentation.case_id],set_=values,where=~and_(*live(at)))
    if (await db.execute(claim)).rowcount!=1:
        raise HTTPException(409,{'code':'viz_presentation_busy','message':'已有成员正在共享此算例的视图'})
    await db.execute(delete(VizViewer).where(VizViewer.case_id==case_id))
    await db.commit()
    return {'version':version,'sequence':0}


@router.put('/frame', status_code=204)
async def update_frame(case_id: int, request: Request, version: str = Query(pattern=r'^[0-9a-f]{32}$'),
                       sequence: int = Query(gt=0, lt=2**63-1),
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id,user,db,write=True)
    if request.headers.get('content-type')!='image/jpeg':
        raise HTTPException(415,'共享画面必须是 JPEG')
    frame = bytearray()
    async for chunk in request.stream():
        if len(frame)+len(chunk)>FRAME_LIMIT:
            raise HTTPException(413,'共享画面超过大小限制')
        frame.extend(chunk)
    if not frame.startswith(b'\xff\xd8\xff') or not frame.endswith(b'\xff\xd9'):
        raise HTTPException(422,'共享画面格式无效')
    at = time.time()
    changed = await db.execute(update(VizPresentation).where(VizPresentation.case_id==case_id,
        VizPresentation.presenter_id==user.id,VizPresentation.version==version,VizPresentation.sequence==sequence-1,
        *live(at)).values(sequence=sequence,frame=bytes(frame),expires_at=at+PRESENTER_SECONDS))
    if changed.rowcount!=1:
        raise stale()
    await db.commit()
    return Response(status_code=204)


@router.get('/frame')
async def frame(case_id: int, version: str = Query(pattern=r'^[0-9a-f]{32}$'), after: int = Query(0, ge=0),
                user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    latest = (await db.execute(select(VizPresentation.sequence, VizPresentation.frame).where(
        VizPresentation.case_id==case_id,VizPresentation.version==version,VizPresentation.sequence>after,
        *live(time.time())))).one_or_none()
    if latest is None or latest.frame is None:
        return Response(status_code=204, headers=FRAME_HEADERS)
    return Response(latest.frame,media_type='image/jpeg',headers=FRAME_HEADERS | {'X-Frame-Sequence':str(latest.sequence)})


@router.post('/presence', status_code=204)
async def presence(case_id: int, version: str = Query(pattern=r'^[0-9a-f]{32}$'),
                   user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    at = time.time()
    present = await db.scalar(select(VizPresentation.case_id).where(
        VizPresentation.case_id==case_id,VizPresentation.version==version,*live(at)))
    if present is None:
        raise stale()
    values = {'version':version,'expires_at':at+VIEWER_SECONDS}
    await db.execute(insert(db,VizViewer).values(case_id=case_id,user_id=user.id,**values).on_conflict_do_update(
        index_elements=[VizViewer.case_id,VizViewer.user_id],set_=values))
    await db.commit()
    return Response(status_code=204)


@router.delete('', status_code=204)
async def stop(case_id: int, version: str = Query(pattern=r'^[0-9a-f]{32}$'),
               user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id,user,db,write=True)
    stopped = await db.execute(update(VizPresentation).where(VizPresentation.case_id==case_id,
        VizPresentation.presenter_id==user.id,VizPresentation.version==version).values(expires_at=0,frame=None))
    if not stopped.rowcount:
        raise stale()
    await db.execute(delete(VizViewer).where(VizViewer.case_id==case_id,VizViewer.version==version))
    await db.commit()
    return Response(status_code=204)


async def discard_expired():
    at = time.time()
    async with SessionLocal() as db:
        await db.execute(delete(VizViewer).where(VizViewer.expires_at<=at))
        await db.execute(update(VizPresentation).where(VizPresentation.expires_at<=at,
            VizPresentation.frame.is_not(None)).values(frame=None))
        await db.commit()
