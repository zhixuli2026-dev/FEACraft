"""Signed, short-lived credentials for a single connection type and target."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac

import jwt

from ..config import get_settings


AUDIENCE = 'vibeflow-connection'
TTL_SECONDS = {'events': 60, 'cadscene': 60, 'terminal': 60, 'viz': 300,
               'terminal.reconnect': 60, 'terminal.close': 60}


def signing_key():
    return hmac.new(get_settings().jwt_secret.encode(), AUDIENCE.encode(), hashlib.sha256).digest()


def issue(user_id: int, scope: str, target: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({'sub': str(user_id), 'aud': AUDIENCE, 'scope': scope, 'target': target,
                       'iat': now, 'exp': now + timedelta(seconds=TTL_SECONDS[scope])},
                      signing_key(), algorithm='HS256')


def resolve(ticket: str, scope: str, target: str) -> int | None:
    if not ticket or len(ticket) > 2048:
        return None
    try:
        claims = jwt.decode(ticket, signing_key(), algorithms=['HS256'], audience=AUDIENCE,
                            options={'require': ['sub', 'aud', 'iat', 'exp', 'scope', 'target']})
        if claims['scope'] != scope or claims['target'] != target or claims['exp'] - claims['iat'] > TTL_SECONDS[scope]:
            return None
        user_id = int(claims['sub'])
        return user_id if user_id > 0 else None
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        return None


def terminal_target(case_id: int, session: str = '') -> str:
    return f'session:{session}' if session else f'case:{case_id}'


def viz_cookie(sid: int) -> str:
    return f'vf_viz_{sid}'
