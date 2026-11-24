"""SCNET OpenAPI account discovery and an application-owned opaque session."""

import asyncio
import base64
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, update

from ..config import get_settings
from ..scnet_models import ScnetAccountSession
from .async_ops import finish_before_cancel
from .llm import decrypt_key, encrypt_key
from .scnet import compact_sign, service_url
from .scnet_client import ScnetClient, ScnetError
from .scnet_detail import redact_detail


_SCNET_API = '/ac/openapi/v2'
_LIFETIME_SECONDS = 24 * 60 * 60
_RENEWAL_SECONDS = 30
_SERVICES = {'hpc': 'hpcUrls', 'ai': 'aiUrls', 'files': 'efileUrls', 'shell': 'eshellUrls'}
_ACCOUNT_FIELDS = ('userName', 'fullName', 'accountName', 'accountStatus', 'accountBalance', 'computerCenter')


def _bad(message):
    return ScnetError({'code':'scnet_account_response', 'message':message})


def _text(value, limit=160):
    return str(value)[:limit] if type(value) in (str, int) else ''


def _header(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and all(33 <= ord(ch) <= 126 for ch in value)


def _data(response, kind):
    if (not isinstance(response, dict) or response.get('code') not in (0, '0', 200, '200')
            or not isinstance(response.get('data'), kind)):
        raise _bad('SCNET 返回无效的账户或授权数据')
    return response['data']


def _token_expiry(token):
    # These are upstream UI hints. SCNET verifies the token on its own requests.
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if not isinstance(claims, dict):
            return None
        stamp = int(claims.get('expireTime') or claims.get('exp'))
        return stamp // 1000 if stamp >= 10**12 else stamp
    except (IndexError, ValueError, TypeError, OverflowError):
        return None


def _service_addresses(data, platform_origin):
    allow_loopback = urlsplit(platform_origin).scheme == 'http'
    result = {}
    for kind, field in _SERVICES.items():
        entries = data.get(field)
        if isinstance(entries, str):
            entries = [{'enable':True, 'url':entries}]
        if not isinstance(entries, list):
            continue
        for candidate in entries:
            if not isinstance(candidate, dict) or str(candidate.get('enable')).lower() != 'true':
                continue
            location = candidate.get('url')
            if not isinstance(location, str):
                continue
            try:
                if not allow_loopback and urlsplit(location).scheme != 'https':
                    continue
                root = service_url(location, kind if kind != 'shell' else 'platform')
            except ValueError:
                continue
            result[kind] = root
            break
    return result


def _platform(token):
    return ScnetClient(get_settings().scnet_platform_origin, '', '', token, service='platform')


async def discover(username, access_key, secret_key):
    if not _header(username, 160) or not _header(access_key, 512) or not _header(secret_key, 1024):
        raise ValueError('请填写有效的 SCNET 用户名与访问密钥')
    fields = {'accessKey':access_key, 'timestamp':str(int(time.time())), 'user':username}
    headers = {**fields, 'signature':compact_sign(fields, secret_key)}
    client = ScnetClient(get_settings().scnet_auth_origin, access_key, secret_key, service='platform')
    rows = _data(await client.request('POST', '/api/user/v3/tokens', headers=headers), list)
    credentials = (access_key, secret_key, headers['signature'], *(
        row['token'] for row in rows if isinstance(row, dict) and isinstance(row.get('token'), str)))
    regions = {}
    platform_token = None
    for row in rows:
        if not isinstance(row, dict) or not (key := _text(row.get('clusterId'))):
            raise _bad('SCNET 授权区域缺少编号')
        token = row.get('token')
        if token is not None and not _header(token, 8192):
            raise _bad('SCNET 返回无效凭证')
        if key == '0':
            platform_token = token
            continue
        if key in regions:
            raise _bad('SCNET 返回重复的授权区域')
        regions[key] = {'name':redact_detail(_text(row.get('clusterName')) or key, (*credentials, token)),
                        'token':token, 'services':{}, 'status':'查询中' if token else '凭证不可用'}

    async def overview():
        if not platform_token:
            return {'userName':username}, '平台凭证不可用'
        try:
            record = _data(await _platform(platform_token).request('GET', _SCNET_API+'/user'), dict)
        except ScnetError:
            return {'userName':username}, '账户概览暂不可用'
        view = {key: _text(record[key]) for key in _ACCOUNT_FIELDS if key in record}
        view.setdefault('userName', username)
        return redact_detail(view, (*credentials, platform_token)), None

    slots = asyncio.Semaphore(6)

    async def discover_region(key, region):
        if not (token := region['token']):
            return
        platform = _platform(token)
        try:
            async with slots:
                value = _data(await platform.request('GET', _SCNET_API+'/center'), (dict, list))
            rows = [value] if isinstance(value, dict) else value
            matches = [row for row in rows if isinstance(row, dict) and _text(row.get('id')) == key]
            if len(matches) != 1:
                raise _bad('授权区域与凭证未匹配')
            region['services'] = _service_addresses(matches[0], platform.base_url)
            region['status'] = '已发现服务' if region['services'] else '无可用服务'
        except (ScnetError, ValueError):
            region['status'] = '服务发现失败'

    result = await asyncio.gather(overview(), *(discover_region(key, data) for key, data in regions.items()))
    account, notice = result[0]
    return {'account': account, 'notice': notice, 'regions': regions}


def _scope(user_id, session_id):
    return (ScnetAccountSession.user_id == user_id, ScnetAccountSession.id == session_id)


def _public(row, state):
    return {'id':row.id,'revision':row.revision,'expires_at':row.expires_at,
            'account':state['account'],'notice':state['notice'],
            'regions':[{'id':key,'name':region['name'],'status':region['status'],
                        'services':list(region['services']), 'can_renew':bool(region['token']),
                        'token_expires_at':_token_expiry(region['token']) if region['token'] else None}
                       for key, region in state['regions'].items()]}


async def create(db, user_id, state):
    now = int(time.time())
    await db.execute(delete(ScnetAccountSession).where(ScnetAccountSession.user_id == user_id,
                                                    ScnetAccountSession.expires_at <= now))
    row = ScnetAccountSession(id=str(uuid4()), user_id=user_id, state_cipher=encrypt_key(json.dumps(state)),
                              revision=1, renewal_until=0, expires_at=now+_LIFETIME_SECONDS)
    db.add(row)
    await db.commit()
    return _public(row, state)


async def load(db, user_id, session_id, revision):
    row = await db.scalar(select(ScnetAccountSession).where(*_scope(user_id, session_id),
        ScnetAccountSession.expires_at > int(time.time())).execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, {'code':'scnet_session_closed','message':'SCNET 会话已结束，请重新连接'})
    if row.revision != revision:
        raise HTTPException(409, {'code':'scnet_session_changed','message':'SCNET 会话已更新，请重新连接'})
    return row


async def logout(db, user_id, session_id):
    await db.execute(delete(ScnetAccountSession).where(*_scope(user_id, session_id)))
    await db.commit()


async def renew(db, user_id, session_id, revision, region_id, request):
    from .request_lifecycle import wait

    row = await load(db, user_id, session_id, revision)
    state = json.loads(decrypt_key(row.state_cipher))
    region = state['regions'].get(region_id, {})
    if not (token := region.get('token')):
        raise ValueError('此区域没有可续约的 SCNET 凭证')
    now = int(time.time())
    predicates = (*_scope(user_id, session_id), ScnetAccountSession.revision == revision)
    await db.rollback()
    claimed = await db.execute(update(ScnetAccountSession).where(*predicates,
        ScnetAccountSession.renewal_until <= now, ScnetAccountSession.expires_at > now)
        .values(renewal_until=now+_RENEWAL_SECONDS))
    await db.commit()
    if not claimed.rowcount:
        raise HTTPException(409, 'SCNET 会话已更新或正在续约')

    async def clear_claim():
        await db.rollback()
        await db.execute(update(ScnetAccountSession).where(*predicates,
            ScnetAccountSession.renewal_until == now+_RENEWAL_SECONDS).values(renewal_until=0))
        await db.commit()

    try:
        result = await wait(_platform(token).request('POST', _SCNET_API+'/tokens/next'), request=request)
        new_token = _data(result, str)
        if not _header(new_token, 8192):
            raise _bad('SCNET 返回无效的续约凭证')
        region['token'] = new_token
        updated = await db.execute(update(ScnetAccountSession).where(*predicates,
            ScnetAccountSession.renewal_until == now+_RENEWAL_SECONDS).values(
                state_cipher=encrypt_key(json.dumps(state)), revision=revision+1, renewal_until=0,
                expires_at=int(time.time())+_LIFETIME_SECONDS))
        await db.commit()
        if not updated.rowcount:
            raise HTTPException(409, 'SCNET 会话已结束或已更新')
        return _public(await load(db, user_id, session_id, revision+1), state)
    finally:
        await finish_before_cancel(clear_claim())


async def regional_client(db, user_id, session_id, revision, region_id, kind):
    row = await load(db, user_id, session_id, revision)
    region = json.loads(decrypt_key(row.state_cipher))['regions'].get(region_id, {})
    root = region.get('services', {}).get(kind)
    if not root or not region.get('token'):
        raise HTTPException(409, '所选区域未发现此服务')
    return ScnetClient(root, '', '', region['token'], service=kind)
