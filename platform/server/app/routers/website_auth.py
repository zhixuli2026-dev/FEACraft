"""Hosted website authentication; provider credentials never reach JavaScript."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from typing import Literal

from ..db import SessionLocal
from ..deps import get_current_user
from ..models import ExternalIdentity, User, WebsiteOAuthTransaction
from ..services import oidc, siwc_protocol as protocol, website_auth as service
from ..services.async_ops import finish_before_cancel

router = APIRouter(prefix='/api/v1/auth', tags=['auth'])


class StartIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intent: Literal['login', 'link'] = 'login'


def configured():
    try:
        return protocol.configuration()
    except protocol.AuthorizationError as error:
        raise HTTPException(503, {'code': error.code, 'message': 'ChatGPT 网站登录尚未配置完成'}) from error


def response_cookie(response, name, value, cfg, seconds):
    response.set_cookie(name, value, path='/', secure=cfg.secure_cookies, httponly=True,
                        samesite='lax', max_age=seconds)
    response.headers['Cache-Control'] = 'no-store'


def clear_cookie(response, name, cfg):
    response.delete_cookie(name, path='/', secure=cfg.secure_cookies, httponly=True, samesite='lax')
    response.headers['Cache-Control'] = 'no-store'


@router.get('/chatgpt/status')
async def status(request: Request):
    try:
        cfg = protocol.configuration()
    except protocol.AuthorizationError as error:
        return JSONResponse({'enabled': False, 'reason': error.code}, headers={'Cache-Control': 'no-store'})
    return JSONResponse({'enabled': True, 'pending': await service.pending(request.cookies.get(protocol.STATE_COOKIE))},
                        headers={'Cache-Control': 'no-store'})


@router.post('/chatgpt/start')
async def start(body: StartIn, request: Request):
    cfg = configured()
    service.require_origin(request, cfg)
    user_id = None
    if body.intent == 'link':
        from ..deps import bearer
        async with SessionLocal() as db:
            user = await get_current_user(request, await bearer(request), db)
            user_id = user.id
    try:
        result, browser = await service.start(cfg, body.intent, user_id, request.cookies.get(protocol.STATE_COOKIE))
    except protocol.AuthorizationError as error:
        raise HTTPException(502, {'code': error.code, 'message': '暂时无法开始 ChatGPT 登录，请稍后重试'}) from error
    response = JSONResponse(result)
    response_cookie(response, protocol.STATE_COOKIE, browser, cfg, 600)
    return response


@router.post('/chatgpt/cancel')
async def cancel(request: Request):
    cfg = configured()
    service.require_origin(request, cfg)
    await service.cancel(request.cookies.get(protocol.STATE_COOKIE))
    response = JSONResponse({'ok': True})
    clear_cookie(response, protocol.STATE_COOKIE, cfg)
    return response


@router.get('/chatgpt/callback')
async def callback(request: Request):
    # Also remove OAuth query values from Uvicorn's response-time access log.
    # Reverse proxies must independently omit callback query strings.
    query = request.query_params
    request.scope['query_string'] = b''
    cfg = configured()
    browser = request.cookies.get(protocol.STATE_COOKIE)
    matched = False
    state, code, provider_error = query.get('state'), query.get('code'), query.get('error')
    if service.browser_hash(state) and service.browser_hash(browser):
        async with SessionLocal() as db:
            matched = await db.scalar(select(WebsiteOAuthTransaction.id).where(
                WebsiteOAuthTransaction.state_sha256 == protocol.digest(state),
                WebsiteOAuthTransaction.browser_sha256 == protocol.digest(browser))) is not None
    token, outcome = None, 'complete'
    try:
        if any(len(query.getlist(name)) > 1 for name in ('state', 'code', 'error')) or (code and provider_error):
            raise protocol.AuthorizationError('callback_invalid')
        token = await finish_before_cancel(service.callback(cfg, state, browser, code, provider_error))
    except protocol.AuthorizationError as error:
        outcome = error.code
    if matched and outcome == 'callback_invalid':
        await service.cancel(browser)
    response = RedirectResponse(cfg.public_origin + '/?chatgpt=' + outcome, status_code=303,
                                headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
    if matched:
        clear_cookie(response, protocol.STATE_COOKIE, cfg)
    if token is not None:
        response_cookie(response, protocol.SESSION_COOKIE, token, cfg, service.SESSION_SECONDS)
    return response


@router.post('/logout')
async def logout(request: Request):
    raw = service.session_cookie(request)
    pending = [request.cookies.get(name) for name in (protocol.STATE_COOKIE,oidc.STATE_COOKIE,oidc.LOCAL_STATE_COOKIE)]
    identity = None
    if raw or any(pending):
        service.require_origin(request)
        identity = await service.logout(raw)
        for browser in filter(None,pending):
            await service.cancel(browser)
    cfg = protocol.Configuration('', '', secure_cookies=True)
    local = protocol.Configuration('', '', secure_cookies=False)
    destination = await oidc.logout_url(identity) if identity else None
    response = JSONResponse({'ok':True, 'logout_url':destination} if destination else {'ok':True})
    clear_cookie(response, protocol.SESSION_COOKIE, cfg)
    clear_cookie(response, protocol.STATE_COOKIE, cfg)
    clear_cookie(response, oidc.STATE_COOKIE, cfg)
    clear_cookie(response, oidc.LOCAL_SESSION_COOKIE, local)
    clear_cookie(response, oidc.LOCAL_STATE_COOKIE, local)
    return response


@router.get('/chatgpt/connections')
async def connections(user: User = Depends(get_current_user)):
    async with SessionLocal() as db:
        rows = (await db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id,
            ExternalIdentity.issuer == protocol.ISSUER,
            ExternalIdentity.revoked_at.is_(None)).order_by(ExternalIdentity.created_at))).all()
        return JSONResponse([{'id': row.id, 'provider': 'openai', 'email': row.display_email,
            'email_verified': row.email_verified, 'created_at': row.created_at.isoformat()} for row in rows],
            headers={'Cache-Control': 'no-store'})


@router.delete('/chatgpt/connections/{identifier}')
async def unlink(identifier: str, user: User = Depends(get_current_user)):
    await service.unlink(identifier, user.id)
    return {'ok': True}
