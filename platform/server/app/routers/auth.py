"""认证路由：注册 / 登录 / 当前用户"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import SessionLocal, get_db
from ..deps import bearer, get_current_user
from ..models import ExternalIdentity, User, WebsiteOAuthTransaction, Workspace
from ..security import create_token, hash_password, verify_password
from ..services import oidc, siwc_protocol as protocol, website_auth
from ..services.async_ops import finish_before_cancel
from .website_auth import StartIn, clear_cookie, response_cookie

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class OidcCallbackIn(BaseModel):
    code: str = Field(max_length=4096)
    state: str = Field(max_length=100)


@router.get("/oidc/providers")
async def oidc_providers():
    return [{"id":item.provider_id,"name":item.name} for item in oidc.configured().values()]


def _oidc_configuration(provider_id):
    try:
        return oidc.configuration(provider_id)
    except protocol.AuthorizationError as error:
        raise HTTPException(404 if error.code=='not_configured' else 503, '组织登录配置不可用') from None


@router.post("/oidc/{provider_id}/start")
async def oidc_start(provider_id: str, request: Request, body: StartIn=StartIn()):
    cfg = _oidc_configuration(provider_id)
    website_auth.require_origin(request,cfg)
    actor_id = None
    if body.intent=='link':
        async with SessionLocal() as db:
            actor_id=(await get_current_user(request,await bearer(request),db)).id
    try:
        result,browser=await website_auth.start(cfg,body.intent,actor_id,request.cookies.get(cfg.state_cookie))
    except protocol.AuthorizationError as error:
        raise HTTPException(502, {'code':error.code,'message':'暂时无法开始组织登录，请稍后重试'}) from None
    response=JSONResponse(result)
    response_cookie(response,cfg.state_cookie,browser,cfg,600)
    return response


async def _oidc_exchange(cfg,state,browser,code,error):
    return await finish_before_cancel(website_auth.callback(cfg,state,browser,code,error,
        exchange=oidc.exchange,configuration=lambda: oidc.configuration(cfg.provider_id)))


@router.get("/oidc/{provider_id}/callback")
async def oidc_redirect_callback(provider_id: str, request: Request):
    query=request.query_params
    request.scope['query_string']=b''
    cfg=_oidc_configuration(provider_id)
    state, browser = query.get('state'), request.cookies.get(cfg.state_cookie)
    matched=False
    if website_auth.browser_hash(state) and website_auth.browser_hash(browser):
        async with SessionLocal() as db:
            matched=await db.scalar(select(WebsiteOAuthTransaction.id).where(
                WebsiteOAuthTransaction.state_sha256==protocol.digest(state),
                WebsiteOAuthTransaction.browser_sha256==protocol.digest(browser))) is not None
    code,error=query.get('code'),query.get('error')
    token,outcome=None,'complete'
    try:
        if any(len(query.getlist(name))>1 for name in ('state','code','error')) or (code and error):
            if matched:
                await website_auth.cancel(browser)
            raise protocol.AuthorizationError('callback_invalid')
        token=await _oidc_exchange(cfg,state,browser,code,error)
    except protocol.AuthorizationError as failure:
        outcome=failure.code
    response=RedirectResponse(cfg.public_origin+'/?oidc='+outcome,status_code=303,
        headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    if matched:
        clear_cookie(response,cfg.state_cookie,cfg)
    if token is not None:
        response_cookie(response,cfg.session_cookie,token,cfg,website_auth.SESSION_SECONDS)
    return response


@router.post("/oidc/default/callback")
async def oidc_legacy_callback(body: OidcCallbackIn, request: Request):
    cfg=_oidc_configuration('default')
    website_auth.require_origin(request,cfg)
    if cfg.redirect_uri != cfg.public_origin+'/oidc/callback':
        raise HTTPException(404,'组织登录回调不可用')
    try:
        token=await _oidc_exchange(cfg,body.state,request.cookies.get(cfg.state_cookie),body.code,None)
    except protocol.AuthorizationError as error:
        raise HTTPException(400,{'code':error.code,'message':'组织登录未完成'}) from None
    response=JSONResponse({'ok':True})
    clear_cookie(response,cfg.state_cookie,cfg)
    response_cookie(response,cfg.session_cookie,token,cfg,website_auth.SESSION_SECONDS)
    return response


@router.get("/oidc/connections")
async def oidc_connections(user: User=Depends(get_current_user), db: AsyncSession=Depends(get_db)):
    rows=await db.scalars(select(ExternalIdentity).where(
        ExternalIdentity.user_id==user.id,ExternalIdentity.issuer!=protocol.ISSUER,
        ExternalIdentity.revoked_at.is_(None)).order_by(ExternalIdentity.created_at))
    choices={(item.issuer,item.client_id):item for item in oidc.configured().values()}
    return JSONResponse([{'id':row.id,'name':item.name if (item:=choices.get((row.issuer,row.client_id))) else '组织登录',
        'email':row.display_email,'email_verified':row.email_verified} for row in rows],
        headers={'Cache-Control':'no-store'})


@router.delete("/oidc/connections/{identifier}")
async def oidc_unlink(identifier: str, user: User=Depends(get_current_user)):
    await website_auth.unlink(identifier,user.id,organization=True)
    return {'ok':True}


@router.post("/register")
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    existing_id = select(User.id).where(User.email == body.email)
    if await db.scalar(existing_id) is not None:
        raise HTTPException(409, "邮箱已注册")
    # The lookup is read-only. Release its connection before the CPU-bound hash.
    # The unique constraint still resolves concurrent registrations for the same email.
    await db.rollback()
    user = User(email=body.email, name=body.name or body.email.split("@")[0],
                password_hash=await asyncio.to_thread(hash_password, body.password))
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as error:
        # A concurrent signup may win after the initial email check.
        await db.rollback()
        if await db.scalar(existing_id) is not None:
            raise HTTPException(409, "邮箱已注册") from error
        raise
    # 默认工作区 + 个人组织（free 订阅，订阅体系骨架）
    db.add(Workspace(user_id=user.id, name="Default"))
    from ..services.quota import ensure_user_org
    await ensure_user_org(db, user)
    await db.commit()
    return {"token": create_token(user.id), "user": {"id": user.id, "email": user.email, "name": user.name}}


@router.post("/login")
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == body.email))
    if user is None or user.status != 'active' or not await asyncio.to_thread(verify_password, body.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码错误")
    return {"token": create_token(user.id), "user": {"id": user.id, "email": user.email, "name": user.name}}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "name": user.name}
