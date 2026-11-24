"""Account-scoped ACP turn admission, streamed-event replay and cancellation."""
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import AcpThread, AcpTurn, AgentEvent, User
from ..services import acp_approval, acp_case_files, acp_execution as execution, conversation_execution as control
from ..services.conversation_history import event_text_field


router = APIRouter(prefix='/api/v1/acp', tags=['acp'])


class TurnIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_profile_id: int | None = Field(default=None, gt=0)
    client_message_id: str = Field(min_length=1, max_length=128, pattern=r'^[^\x00]+$')
    content: str = Field(min_length=1, max_length=256_000)
    thread_id: str | None = Field(default=None, min_length=36, max_length=36)
    allow_drafts: bool | None = None
    case_files: list[Annotated[str, Field(min_length=1, max_length=384)]] = Field(
        default_factory=list, max_length=acp_case_files.MAX_FILES)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'reject']


class PublishIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    draft: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=384)


@router.get('/conversations/{cid}/capabilities')
async def capabilities(cid: int, user: User = Depends(get_current_user), db=Depends(get_db)):
    await execution.owned_case(db, cid, user, write=False)
    return {'case_input_limit': acp_case_files.MAX_FILES if get_settings().acp_docker_image else 0}


@router.post('/conversations/{cid}/{harness}/turns', status_code=202)
async def submit(cid: int, harness: str, body: TurnIn, user: User = Depends(get_current_user), db=Depends(get_db)):
    if not body.content.strip() or '\0' in body.content:
        raise HTTPException(422, '请输入有效消息')
    return await execution.submit(db, cid, user, harness, profile_id=body.provider_profile_id,
                                  client_id=body.client_message_id, content=body.content, thread_id=body.thread_id,
                                  allow_drafts=body.allow_drafts, case_files=body.case_files)


@router.get('/conversations/{cid}/{harness}/turns')
async def turns(cid: int, harness: str, limit: int = Query(default=20, ge=1, le=50),
                user: User = Depends(get_current_user), db=Depends(get_db)):
    await execution.owned_case(db, cid, user, write=False)
    rows = await db.execute(select(AcpTurn, AcpThread).join(AcpThread, AcpThread.id == AcpTurn.thread_id)
        .where(AcpTurn.conversation_id == cid, AcpTurn.created_by == user.id, AcpThread.harness == harness)
        .order_by(AcpTurn.created_at.desc(), AcpTurn.id.desc()).limit(limit))
    return [execution.public(turn, thread) for turn, thread in rows]


@router.get('/turns/{identifier}')
async def turn(identifier: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    turn_row, thread = await execution.lookup(db, identifier, user)
    return {**execution.public(turn_row, thread), 'prompt':await execution.prompt_text(db, turn_row)}


@router.get('/turns/{identifier}/events')
async def events(identifier: str, after: int = Query(default=0, ge=0),
                 limit: int = Query(default=100, ge=1, le=200),
                 user: User = Depends(get_current_user), db=Depends(get_db)):
    turn_row, thread = await execution.lookup(db, identifier, user)
    rows = (await db.scalars(select(AgentEvent).where(AgentEvent.conversation_id == turn_row.conversation_id,
        AgentEvent.type == 'acp', event_text_field(db, 'turn_id') == identifier, AgentEvent.seq > after)
        .order_by(AgentEvent.seq).limit(limit + 1))).all()
    return {'turn': execution.public(turn_row, thread), 'prompt': await execution.prompt_text(db, turn_row) if after == 0 else None, 'events': [
        {**json.loads(event.payload), 'seq':event.seq} for event in rows[:limit]],
        'has_more': len(rows) > limit}


@router.post('/turns/{identifier}:cancel')
async def cancel(identifier: str, user: User = Depends(get_current_user), db=Depends(get_db)):
    turn_row, thread = await execution.lookup(db, identifier, user)
    result = execution.public(turn_row, thread)
    if turn_row.phase in execution.TERMINAL:
        return result
    cid, user_id = turn_row.conversation_id, user.id
    await db.rollback()
    requested = await control.request_stop(cid, user_id, run_id=identifier)
    if requested and (local := execution.ACTIVE.get(identifier)) is not None:
        local.cancelled.set()
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or user.status != 'active':
        raise HTTPException(401, '用户不可用')
    turn_row, thread = await execution.lookup(db, identifier, user)
    return {**execution.public(turn_row, thread), 'stop_requested':requested}


@router.post('/turns/{identifier}/approvals/{approval_id}')
async def decide(identifier: str, approval_id: str, body: DecisionIn,
                 user: User = Depends(get_current_user), db=Depends(get_db)):
    return await acp_approval.decide(db, identifier, approval_id, body.decision, user)


@router.post('/turns/{identifier}/drafts:publish')
async def publish(identifier: str, body: PublishIn, user: User = Depends(get_current_user), db=Depends(get_db)):
    return await acp_case_files.publish(db, identifier, body.draft, body.path, user)
