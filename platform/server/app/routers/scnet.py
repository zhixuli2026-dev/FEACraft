"""SCNET B/S 操作路由；凭证只在请求内使用，不写日志。"""
import asyncio
import threading
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import Membership, User
from ..services.scnet_client import ScnetClient, ScnetError
from ..services.scnet import require_confirmation
from ..services.scnet_detail import copy_summary, detail_model, redact_detail
from ..services import request_deadline, request_lifecycle, scnet_account, scnet_download
from ..services.async_ops import finish_before_cancel
from ..services.case_write import CaseWriteConflict

class ScnetRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def safe_validation(request):
            try:
                return await handle(request)
            except RequestValidationError:
                raise HTTPException(422, 'SCNET 请求格式无效') from None

        return safe_validation


router = APIRouter(prefix="/api/v1/scnet", tags=["scnet"], route_class=ScnetRoute)


class ScnetIn(BaseModel):
    base_url: str = ''
    ai_base_url: str | None = None
    access_key: str = ''
    secret_key: str = ''
    token: str = ""
    session_id: str | None = None
    revision: int | None = None
    region_id: str | None = None
    args: dict = {}
    confirmed: bool = False
    detail_view: bool = False


async def _require_admin(db, user_id):
    admin = await db.scalar(select(Membership.id).where(
        Membership.user_id == user_id, Membership.role == "admin").limit(1))
    if admin is None:
        raise HTTPException(403, "仅组织 admin 可操作 SCNET 资源")


def _limit(error):
    return HTTPException(413, {'code':'scnet_download_too_large','message':str(error)})


class AccountLogin(BaseModel):
    username: str
    access_key: SecretStr
    secret_key: SecretStr


class AccountReference(BaseModel):
    session_id: str


class AccountRenewal(AccountReference):
    revision: int
    region_id: str


@router.post('/account:connect')
async def connect_account(body: AccountLogin, request: Request, response: Response,
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user_id = user.id
    await _require_admin(db, user_id)
    await db.rollback()
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        state = await request_lifecycle.wait(scnet_account.discover(
            body.username, body.access_key.get_secret_value(), body.secret_key.get_secret_value()), request=request)
        await _require_admin(db, user_id)
        return await scnet_account.create(db, user_id, state)
    except ScnetError as error:
        raise HTTPException(502, error.detail) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post('/account:renew')
async def renew_account(body: AccountRenewal, request: Request, response: Response,
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user_id = user.id
    await _require_admin(db, user_id)
    response.headers['Cache-Control'] = 'private, no-store'
    try:
        return await scnet_account.renew(db, user_id, body.session_id, body.revision, body.region_id, request)
    except ScnetError as error:
        raise HTTPException(502, error.detail) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post('/account:logout')
async def logout_account(body: AccountReference, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    await scnet_account.logout(db, user.id, body.session_id)
    return {'closed': True}


@router.post('/files:download')
async def download(body: ScnetIn, request: Request, user: User=Depends(get_current_user),
                   db: AsyncSession=Depends(get_db)):
    user_id = user.id
    await _require_admin(db, user_id)
    if body.session_id is not None:
        raise HTTPException(409, '账户连接暂未接入文件下载')
    await db.rollback()
    path = body.args.get('path')
    if (not isinstance(path, str) or not path or len(path) > 4096
            or any(ord(char) < 32 for char in path)):
        raise HTTPException(400, 'SCNET 下载路径无效')
    output = request.state.scnet_download
    deadline = request_deadline.current()
    stopped = threading.Event()

    def check():
        deadline.check()
        if stopped.is_set():raise InterruptedError('SCNET download disconnected')

    async def prepare(client):
        operation = asyncio.create_task(asyncio.to_thread(scnet_download.spool, client, path, output, check))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            stopped.set()
            await finish_before_cancel(asyncio.gather(operation, return_exceptions=True))
            raise

    try:
        client = ScnetClient(body.base_url, body.access_key, body.secret_key, body.token)
        size, zipped = await request_lifecycle.wait(prepare(client), request=request)
    except scnet_download.DownloadLimit as error:
        raise _limit(error) from error
    except scnet_download.DownloadProtocol as error:
        raise HTTPException(502, {'code':'scnet_download_invalid','message':str(error)}) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except ScnetError as error:
        raise HTTPException(502, error.detail) from error
    actor = await db.get(User, user_id, populate_existing=True)
    if actor is None or actor.status != 'active':
        raise HTTPException(403, '用户不可用')
    await _require_admin(db, user_id)
    await db.rollback()
    check()
    name = path.replace('\\','/').rstrip('/').rsplit('/',1)[-1]
    if name in {'','.','..'}:name='download'
    if zipped:name += '.zip'
    return StreamingResponse(output.chunks(), media_type='application/zip' if zipped else 'application/octet-stream',
        headers={'Content-Length':str(size),'Content-Disposition':"attachment; filename*=UTF-8''"+quote(name,safe=''),
                 'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})


@router.post("/{operation}")
async def execute(operation: str, body: ScnetIn, request: Request,
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user_id = user.id
    await _require_admin(db, user_id)
    try:
        resource, _, action = operation.partition('.')
        ai = resource in {'containers', 'notebooks'}
        kind = 'ai' if ai else 'hpc'
        managed = body.session_id is not None
        if managed:
            if body.revision is None or not body.region_id:
                raise ValueError('需要 SCNET 会话版本和区域编号')
            client = await scnet_account.regional_client(db, user_id, body.session_id, body.revision, body.region_id, kind)
            if operation not in {'jobs.list', 'desktops.list'} and not (
                body.detail_view and operation in {'jobs.detail', 'jobs.history-detail', 'containers.detail',
                                                   'notebooks.detail', 'desktops.detail'}
            ):
                raise HTTPException(409, '此操作暂未接入账户连接')
        else:
            root = (body.ai_base_url or body.base_url) if ai else body.base_url
            client = ScnetClient(root, body.access_key, body.secret_key, body.token, service=kind)
        await db.rollback()
        require_confirmation(operation, body.confirmed)
        secrets = (client.access_key, client.secret_key, client.token)
        if body.detail_view:
            work = client.detail(operation, body.args)
        elif managed:
            work = client.account_list(operation)
        else:
            work = client.execute(operation, body.args)
        result = await request_lifecycle.wait(work, request=request)
        if managed:
            await _require_admin(db, user_id)
            await scnet_account.load(db, user_id, body.session_id, body.revision)
        if body.detail_view:
            try:
                model = detail_model(result, resource, secrets)
            except ValueError as error:
                raise ScnetError({'message': str(error)}) from error
            return {**model, 'copy_text': copy_summary(model)}
        return redact_detail(result, secrets) if action == 'detail' or managed else result
    except scnet_download.DownloadLimit as error:
        raise _limit(error) from error
    except scnet_download.DownloadProtocol as error:
        raise HTTPException(502, {'code':'scnet_download_invalid','message':str(error)}) from error
    except ValueError as error:
        raise HTTPException(400, str(error))
    except ScnetError as error:
        raise HTTPException(502, error.detail) from error
