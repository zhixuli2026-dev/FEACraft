"""Hosted native inference uses a versioned, authorized platform API profile."""
import hashlib
import json
import re
from urllib.parse import urlsplit

from fastapi import HTTPException

from ..models import ProviderProfile, User
from . import llm, profile_sharing
from .workflow_state import ExecutionConflict

TOKEN_ENV = 'VIBEFLOW_CODEX_PROVIDER_TOKEN'
SCHEMA = 'vibeflow.cli-api-credential.v1'


def endpoint(provider, base_url):
    info = llm.PROVIDERS.get(provider)
    if info is None or info['api'] != 'openai':
        raise HTTPException(400, '原生 CLI 需要 Responses 服务，不能使用 Messages 协议配置')
    value = (base_url or info['base_url']).rstrip('/')
    parsed = urlsplit(value)
    if (not value or len(value) > 300 or any(ord(c) <= 32 for c in value)
            or parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or (parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'})
            or parsed.path.endswith(('/chat/completions', '/responses', '/messages'))):
        raise HTTPException(400, '原生 CLI 需要无凭据、无查询参数的 API 基础地址')
    try:
        parsed.port
    except ValueError as error:
        raise HTTPException(400, '无效的 API 端口') from error
    return value


def descriptor(profile):
    base_url = endpoint(profile.provider, profile.base_url)
    model = profile.default_model or ''
    if not isinstance(model, str) or len(model) > 120 or any(ord(c) < 32 for c in model):
        raise HTTPException(400, '无效的原生 CLI 默认模型')
    # Hash ciphertext, never the plaintext API key. Rotation of even the same
    # key intentionally retires the old scope. Display-name edits are harmless.
    revision = hashlib.sha256(json.dumps([SCHEMA, profile.id, profile.user_id,
        profile.provider, profile.base_url, model, profile.key_ciphertext],
        ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return {'schema': SCHEMA, 'profile_id': profile.id, 'owner_id': profile.user_id,
            'provider': profile.provider, 'base_url': base_url, 'default_model': model,
            'wire_api': 'responses', 'revision': revision}


def validate(value):
    fields = {'schema', 'profile_id', 'owner_id', 'provider', 'base_url', 'default_model', 'wire_api', 'revision'}
    if (not isinstance(value, dict) or set(value) != fields or value['schema'] != SCHEMA
            or value['wire_api'] != 'responses'
            or any(type(value[key]) is not int or value[key] <= 0 for key in ('profile_id', 'owner_id'))
            or not isinstance(value['provider'], str) or len(value['provider']) > 40
            or not isinstance(value['base_url'], str)
            or not isinstance(value['default_model'], str) or len(value['default_model']) > 120
            or any(ord(c) < 32 for c in value['default_model'])
            or not isinstance(value['revision'], str) or not re.fullmatch('[0-9a-f]{64}', value['revision'])):
        raise ValueError('Invalid hosted CLI credential binding')
    try:
        if endpoint(value['provider'], value['base_url']) != value['base_url']:
            raise ValueError('Noncanonical hosted CLI endpoint')
    except HTTPException as error:
        raise ValueError('Invalid hosted CLI endpoint') from error
    return value


async def accessible(db, profile_id, user_id):
    profile = await db.get(ProviderProfile, profile_id, populate_existing=True)
    if profile is None or profile.org_id is not None or not await profile_sharing.can_access(db, profile, user_id):
        raise HTTPException(403, '原生 CLI 的 API 配置不可用或共享已撤销')
    owner = await db.get(User, profile.user_id, populate_existing=True)
    if owner is None or owner.status != 'active':
        raise HTTPException(403, 'API 配置所有者已不可用')
    return profile


async def select(db, profile_id, user_id, wire_api):
    if profile_id is None:
        if wire_api is not None:
            raise HTTPException(400, 'Responses 协议选择需要 API 配置')
        return None, None
    profile = await accessible(db, profile_id, user_id)
    selected = descriptor(profile)
    official = profile.provider == 'openai' and selected['base_url'] == 'https://api.openai.com/v1'
    if wire_api != 'responses' and not official:
        raise HTTPException(400, '请明确选择 Responses 协议，并确认此服务支持 Responses API')
    try:
        secret = llm.decrypt_key(profile.key_ciphertext)
    except Exception as error:
        raise HTTPException(409, 'API 凭据不可解密，请重新配置密钥') from error
    if not 1 <= len(secret) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
        raise HTTPException(400, 'API 密钥必须是有效的单行凭据')
    return selected, secret


async def check(db, selected, user_id):
    validate(selected)
    profile = await accessible(db, selected['profile_id'], user_id)
    if descriptor(profile) != selected:
        raise ExecutionConflict('input_stale', 'API 凭据或服务配置已变化，请创建新的原生会话')
    return profile


def overrides(selected):
    validate(selected)
    return {
        'model_provider': 'vibeflow_api',
        'model_providers.vibeflow_api': {'name': 'VibeFlow API', 'base_url': selected['base_url'],
            'env_key': TOKEN_ENV, 'wire_api': 'responses', 'requires_openai_auth': False,
            'supports_websockets': False, 'request_max_retries': 0, 'stream_max_retries': 0},
        'shell_environment_policy': {'inherit': 'core', 'ignore_default_excludes': False,
            'filters': {TOKEN_ENV: 'exclude'}},
        'allow_login_shell': False,
    }


def toml(value):
    if isinstance(value, dict):
        return '{ ' + ', '.join(json.dumps(k) + ' = ' + toml(v) for k, v in value.items()) + ' }'
    return json.dumps(value, ensure_ascii=False)


def config(selected):
    return 'cli_auth_credentials_store = "file"\n' + ''.join(
        key + ' = ' + toml(value) + '\n' for key, value in overrides(selected).items())


def public(selected):
    if selected is None:
        return {'mode': 'unbound', 'inference_configured': False}
    validate(selected)
    return {'mode': 'api_key', 'inference_configured': True, 'provider_profile_id': selected['profile_id'],
            'provider': selected['provider'], 'wire_api': selected['wire_api']}
