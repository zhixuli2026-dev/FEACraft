"""Account-scoped model and reasoning preferences for Agent backends."""
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import ConversationModelPreference, ProviderProfile, User
from ..services import audit, codex_conversations, profile_sharing
from ..services.async_ops import finish_before_cancel
from .codex import NativeModelOptionsIn


router = APIRouter(prefix='/api/v1', tags=['llm'])
Runtime = Literal['api', 'windows', 'wsl', 'docker']


class PreferenceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_profile_id: int = Field(gt=0)
    model: str = Field(default='', max_length=120)
    reasoning_effort: Literal['off', 'low', 'medium', 'high', 'max'] | None = None
    native_options: NativeModelOptionsIn | None = None


def fingerprint(profile):
    text = json.dumps([profile.provider, profile.base_url, profile.default_model, profile.key_ciphertext],
                      ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@router.get('/conversations/{cid}/model-preferences')
async def preferences(cid: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await codex_conversations.authorize(db, cid, user.id)
    rows = (await db.execute(select(ConversationModelPreference, ProviderProfile)
        .join(ProviderProfile, ProviderProfile.id == ConversationModelPreference.provider_profile_id)
        .where(ConversationModelPreference.conversation_id == cid, ConversationModelPreference.user_id == user.id))).all()
    current, stale = {}, []
    for preference, profile in rows:
        if not await profile_sharing.can_access(db, profile, user.id) or fingerprint(profile) != preference.profile_fingerprint:
            stale.append(preference.runtime)
            continue
        current[preference.runtime] = {'provider_profile_id': profile.id, 'model': preference.model,
                                       **json.loads(preference.options_json)}
    return {'preferences': current, 'stale': sorted(stale)}


@router.put('/conversations/{cid}/model-preferences/{runtime}')
async def save_preference(cid: int, runtime: Runtime, body: PreferenceIn, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    conversation = await codex_conversations.authorize(db, cid, user.id)
    profile = await db.get(ProviderProfile, body.provider_profile_id, populate_existing=True)
    if profile is None or not await profile_sharing.can_access(db, profile, user.id):
        raise HTTPException(404, '模型配置不存在')
    result = await store_preference(db, cid, user.id, runtime, body, profile)
    await audit.session_event(db, conversation.case_id, user.id, 'agent.model_preferences.updated',
                             'conversation', cid, {'runtime': runtime, 'provider_profile_id': profile.id})
    await finish_before_cancel(db.commit())
    return result


async def store_preference(db, cid, user_id, runtime, body, profile):
    if (runtime == 'api' and body.native_options is not None) or (runtime != 'api' and body.reasoning_effort is not None):
        raise HTTPException(422, '推理选项需要与执行后端匹配')
    options = body.model_dump(exclude={'provider_profile_id', 'model'}, exclude_none=True)
    values = {'provider_profile_id': profile.id, 'model': body.model.strip(), 'profile_fingerprint': fingerprint(profile),
              'options_json': json.dumps(options, separators=(',', ':'))}
    insert = pg_insert if db.get_bind().dialect.name == 'postgresql' else sqlite_insert
    statement = insert(ConversationModelPreference).values(conversation_id=cid, user_id=user_id, runtime=runtime, **values)
    await db.execute(statement.on_conflict_do_update(index_elements=['conversation_id', 'user_id', 'runtime'], set_=values))
    return {'provider_profile_id': profile.id, 'model': values['model'], **options}
