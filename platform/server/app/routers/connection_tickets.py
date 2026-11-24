"""Authorize individual browser connections before putting credentials on them."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import connection_tickets as tickets
from . import terminal, viz
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1', tags=['connection-tickets'])


class TicketRequest(BaseModel):
    scope: Literal['events', 'cadscene', 'terminal', 'viz']
    case_id: int = Field(default=0, ge=0)
    session_id: str = Field(default='', max_length=48)


@router.post('/connection-tickets')
async def authorize(body: TicketRequest, request: Request, response: Response,
                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    ttl = tickets.TTL_SECONDS[body.scope]
    if body.scope == 'viz':
        if body.case_id or not body.session_id.isascii() or not body.session_id.isdecimal():
            raise HTTPException(400, '请指定可视化会话')
        sid = int(body.session_id)
        if not 0 < sid < 2**63:
            raise HTTPException(400, '无效的可视化会话')
        row = await viz._row(sid, user.id)
        if row is None or row.phase != 'active':
            raise HTTPException(404, '可视化会话不存在或已退出')
        response.set_cookie(tickets.viz_cookie(sid), tickets.issue(user.id, 'viz', str(sid)),
                            max_age=ttl, path=f'/viz-proxy/{sid}/', httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https')
        return {'url': f'/viz-proxy/{sid}/index.html', 'expires_in': ttl}

    if body.scope == 'terminal':
        if body.session_id:
            row = await terminal._session_row(body.session_id, user.id)
            if row is None or (body.case_id and body.case_id != row.case_id):
                raise HTTPException(404, '终端会话不存在')
        elif body.case_id:
            await get_owned_case(body.case_id, user, db, write=False)
        target = tickets.terminal_target(body.case_id, body.session_id)
    else:
        if not body.case_id or body.session_id:
            raise HTTPException(400, '请指定算例')
        await get_owned_case(body.case_id, user, db, write=False)
        target = str(body.case_id)
    return {'ticket': tickets.issue(user.id, body.scope, target), 'expires_in': ttl}
