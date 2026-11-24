"""FastAPI 依赖注入"""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_db
from .models import User
from .security import decode_token

bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if cred is not None:
        uid = decode_token(cred.credentials)
        if uid is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或过期")
        user = await db.get(User, uid)
    else:
        from .services import website_auth, siwc_protocol
        raw = website_auth.session_cookie(request)
        if raw and request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            try:
                website_auth.require_origin(request)
            except siwc_protocol.AuthorizationError as error:
                raise HTTPException(401, '登录配置已变化，请重新登录') from error
        user = await website_auth.cookie_user(db, raw)
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不可用")
    return user


async def connection_user_id(connection, token='', *, scope: str, target: str):
    """Authenticate manual HTTP/WS entrances without exporting cookie sessions."""
    from .db import SessionLocal
    from .services import connection_tickets, website_auth, siwc_protocol
    async with SessionLocal() as db:
        if token:
            uid = connection_tickets.resolve(token, scope, target)
            user = await db.get(User, uid) if uid is not None else None
        else:
            authorization = connection.headers.get('authorization', '')
            scheme, _, bearer_token = authorization.partition(' ')
            if scheme.lower() == 'bearer' and not (scope == 'viz' and connection.cookies.get(siwc_protocol.SESSION_COOKIE)):
                uid = decode_token(bearer_token.strip())
                user = await db.get(User, uid) if uid is not None else None
                return user.id if user is not None and user.status == 'active' else None
            if connection.scope['type'] == 'websocket':
                try:
                    website_auth.require_origin(connection)
                except (HTTPException, siwc_protocol.AuthorizationError):
                    return None
            user = await website_auth.cookie_user(db, website_auth.session_cookie(connection))
        return user.id if user is not None and user.status == 'active' else None
