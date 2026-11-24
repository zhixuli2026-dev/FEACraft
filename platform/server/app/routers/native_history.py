"""Native session history and explicit manual acknowledgement; no launch or inference."""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ..deps import get_current_user
from ..models import User
from ..services import native_session_review as review

router = APIRouter(prefix='/api/v1/native-sessions', tags=['native-session-history'])
Kind = Literal['terminal', 'cli', 'viz']


class RetainIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['retain_for_review']
    snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    note: str = Field(default='', max_length=2000, pattern=r'^[^\x00]*$')


class LinkIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['link_existing_successor']
    successor_id: str = Field(min_length=1, max_length=36)
    source_snapshot_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    successor_identity_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    note: str = Field(default='', max_length=2000, pattern=r'^[^\x00]*$')


@router.get('/{kind}')
async def history(kind: Kind, case_id: int | None = Query(default=None, gt=0),
                  cursor: str | None = Query(default=None, max_length=512),
                  limit: int = Query(default=50, ge=1, le=100), user: User = Depends(get_current_user)):
    return await review.listing(kind, user.id, case_id, cursor, limit)


@router.get('/{kind}/{identifier}')
async def options(kind: Kind, identifier: str, user: User = Depends(get_current_user)):
    return await review.options(kind, identifier, user.id)


@router.post('/{kind}/{identifier}:retain-for-review')
async def retain(kind: Kind, identifier: str, body: RetainIn, user: User = Depends(get_current_user)):
    return await review.retain(kind, identifier, user.id, body.snapshot_sha256, body.note)


@router.get('/{kind}/{identifier}/successors/{successor_id}')
async def link_plan(kind: Kind, identifier: str, successor_id: str, user: User = Depends(get_current_user)):
    from ..services import native_session_lineage as lineage
    return await lineage.plan(kind, identifier, successor_id, user.id)


@router.post('/{kind}/{identifier}:link-successor')
async def link_successor(kind: Kind, identifier: str, body: LinkIn, user: User = Depends(get_current_user)):
    from ..services import native_session_lineage as lineage
    return await lineage.link(kind, identifier, body.successor_id, user.id,
        body.source_snapshot_sha256, body.successor_identity_sha256, body.note)
