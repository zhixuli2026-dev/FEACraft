"""Website Sign in with ChatGPT: pinned discovery, PKCE and verified ID tokens."""
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import secrets
import time
from urllib.parse import quote_plus, urlencode, urlsplit

from cryptography.fernet import Fernet
import httpx
import jwt

from ..config import get_settings

CALLBACK_PATH = '/api/v1/auth/chatgpt/callback'
ISSUER = 'https://auth.openai.com'
STATE_COOKIE = '__Host-vf_chatgpt_state'
SESSION_COOKIE = '__Host-vf_session'
_CACHE = {}


class AuthorizationError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def digest(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


@dataclass(frozen=True)
class Configuration:
    client_id: str
    public_origin: str
    token_auth_method: str = 'none'
    client_secret: str = field(default='', repr=False)
    issuer: str = ISSUER
    secure_cookies: bool = True

    @property
    def redirect_uri(self):
        return self.public_origin + CALLBACK_PATH

    @property
    def fingerprint(self):
        value = [self.client_id, self.public_origin, self.issuer, self.token_auth_method, self.client_secret]
        return hmac.new(get_settings().jwt_secret.encode(),
            json.dumps(value, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()


def configuration():
    settings = get_settings()
    if not settings.siwc_client_id:
        raise AuthorizationError('not_configured')
    origin = settings.siwc_public_origin.rstrip('/')
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError as error:
        raise AuthorizationError('invalid_configuration') from error
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or len(origin) > 300
            or any(ord(c) < 33 for c in origin)
            or not 1 <= len(settings.siwc_client_id) <= 200
            or any(ord(c) < 33 or ord(c) > 126 for c in settings.siwc_client_id)
            or len(settings.jwt_secret) < 32 or settings.jwt_secret == 'dev-only-secret-change-me'):
        raise AuthorizationError('invalid_configuration')
    method, secret = settings.siwc_token_auth_method, settings.siwc_client_secret
    if method not in {'none', 'client_secret_basic'} or (method == 'none' and secret) or (method == 'client_secret_basic' and not secret):
        raise AuthorizationError('invalid_configuration')
    return Configuration(settings.siwc_client_id, origin, method, secret)


def vault():
    # Purpose-separated encryption of ten-minute PKCE state. Rotating the app
    # secret invalidates pending flows; tokens and authorization codes are never stored.
    key = hmac.new(get_settings().jwt_secret.encode(), b'vibeflow.website-pkce.v1', hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def random_value():
    return secrets.token_urlsafe(32)


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')


async def document(url, *, form=None, headers=None):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False) as client:
            async with client.stream('GET' if form is None else 'POST', url, data=form, headers=headers) as response:
                if response.status_code != 200:
                    raise AuthorizationError('provider_unavailable' if form is None else 'token_exchange_failed')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        raise AuthorizationError('provider_response_invalid')
                result = json.loads(data)
    except AuthorizationError:
        raise
    except (httpx.HTTPError, ValueError, UnicodeError) as error:
        raise AuthorizationError('provider_unavailable') from error
    if not isinstance(result, dict):
        raise AuthorizationError('provider_response_invalid')
    return result


def trusted_endpoint(value, cfg):
    if not isinstance(value, str) or len(value) > 2048:
        raise AuthorizationError('discovery_invalid')
    parsed, issuer = urlsplit(value), urlsplit(cfg.issuer)
    permitted = {f'{issuer.scheme}://{issuer.netloc}', *getattr(cfg,'endpoint_origins',())}
    if (f'{parsed.scheme}://{parsed.netloc}' not in permitted or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(ord(c) < 33 for c in value)):
        raise AuthorizationError('discovery_invalid')
    return value


async def discovery(cfg):
    key = ('discovery', cfg.issuer, tuple(getattr(cfg,'endpoint_origins',())))
    cached = _CACHE.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    value = await document(cfg.issuer + '/.well-known/openid-configuration')
    if (value.get('issuer') != cfg.issuer or 'code' not in value.get('response_types_supported', [])
            or 'S256' not in value.get('code_challenge_methods_supported', [])):
        raise AuthorizationError('discovery_invalid')
    for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
        trusted_endpoint(value.get(name), cfg)
    algorithms = value.get('id_token_signing_alg_values_supported')
    if not isinstance(algorithms, list) or not set(algorithms) & {'RS256', 'ES256'}:
        raise AuthorizationError('discovery_invalid')
    _CACHE[key] = (time.monotonic() + 300, value)
    return value


async def authorization_url(cfg, state, verifier, nonce):
    metadata = await discovery(cfg)
    return metadata['authorization_endpoint'] + '?' + urlencode({
        'response_type': 'code', 'client_id': cfg.client_id, 'redirect_uri': cfg.redirect_uri,
        'scope': 'openid profile email', 'state': state, 'nonce': nonce,
        'code_challenge': challenge(verifier), 'code_challenge_method': 'S256'})


async def key_for(cfg, metadata, header):
    kid, algorithm = header.get('kid'), header.get('alg')
    if (not isinstance(kid, str) or not 1 <= len(kid) <= 128 or algorithm not in {'RS256', 'ES256'}
            or algorithm not in metadata['id_token_signing_alg_values_supported'] or header.get('crit')):
        raise AuthorizationError('identity_invalid')
    key = ('jwks', metadata['jwks_uri'])
    for refresh in (False, True):
        cached = _CACHE.get(key)
        if refresh or not cached or cached[0] <= time.monotonic():
            value = await document(metadata['jwks_uri'])
            if not isinstance(value.get('keys'), list) or not 1 <= len(value['keys']) <= 32:
                raise AuthorizationError('identity_invalid')
            cached = (time.monotonic() + 300, value)
            _CACHE[key] = cached
        candidates = [item for item in cached[1]['keys'] if isinstance(item, dict) and item.get('kid') == kid]
        if candidates:
            if len(candidates) != 1:
                raise AuthorizationError('identity_invalid')
            item = candidates[0]
            if (item.get('use', 'sig') != 'sig' or item.get('alg', algorithm) != algorithm
                    or 'verify' not in item.get('key_ops', ['verify'])
                    or item.get('kty') != ('RSA' if algorithm == 'RS256' else 'EC')):
                raise AuthorizationError('identity_invalid')
            try:
                return jwt.PyJWK.from_dict(item, algorithm=algorithm).key
            except (ValueError, jwt.PyJWTError) as error:
                raise AuthorizationError('identity_invalid') from error
    raise AuthorizationError('identity_invalid')


async def exchange(cfg, code, verifier, nonce_sha256, *, userinfo=False):
    metadata = await discovery(cfg)
    form = {'grant_type': 'authorization_code', 'code': code, 'client_id': cfg.client_id,
            'redirect_uri': cfg.redirect_uri, 'code_verifier': verifier}
    headers = {'Accept': 'application/json'}
    if cfg.token_auth_method == 'client_secret_basic':
        encoded = quote_plus(cfg.client_id) + ':' + quote_plus(cfg.client_secret)
        headers['Authorization'] = 'Basic ' + base64.b64encode(encoded.encode('utf8')).decode('ascii')
    elif cfg.token_auth_method == 'client_secret_post':
        form['client_secret'] = cfg.client_secret
    tokens = await document(metadata['token_endpoint'], form=form, headers=headers)
    token = tokens.get('id_token')
    if not isinstance(token, str) or not 1 <= len(token) <= 32768:
        raise AuthorizationError('identity_invalid')
    try:
        header = jwt.get_unverified_header(token)
        key = await key_for(cfg, metadata, header)
        claims = jwt.decode(token, key, algorithms=[header['alg']], issuer=cfg.issuer, audience=cfg.client_id,
            leeway=5, options={'require': ['iss', 'sub', 'aud', 'exp', 'iat', 'nonce']})
        if (any(type(claims[name]) not in (int, float) or not math.isfinite(claims[name]) for name in ('exp', 'iat'))
                or claims['exp'] <= claims['iat']
                or not isinstance(claims['sub'], str) or not 1 <= len(claims['sub']) <= 255
                or any(ord(c) < 33 for c in claims['sub'])
                or not isinstance(claims['nonce'], str)
                or not hmac.compare_digest(digest(claims['nonce']), nonce_sha256)
                or (isinstance(claims['aud'], list) and len(claims['aud']) > 1 and claims.get('azp') != cfg.client_id)
                or ('azp' in claims and claims['azp'] != cfg.client_id)
                or ('sid' in claims and (not isinstance(claims['sid'], str) or not 1 <= len(claims['sid'].strip()) <= 255))):
            raise AuthorizationError('identity_invalid')
    except (ValueError, TypeError, KeyError, jwt.PyJWTError) as error:
        if isinstance(error, AuthorizationError):
            raise
        raise AuthorizationError('identity_invalid') from error
    if userinfo and (not claims.get('email') or claims.get('email_verified') is None):
        access_token = tokens.get('access_token')
        if (not isinstance(access_token,str) or not 1<=len(access_token)<=16384
                or any(not 33<=ord(c)<=126 for c in access_token)):
            raise AuthorizationError('identity_invalid')
        endpoint = trusted_endpoint(metadata.get('userinfo_endpoint'),cfg)
        info = await document(endpoint,headers={'Authorization':f'Bearer {access_token}'})
        if info.get('sub') != claims['sub'] or (claims.get('email') and info.get('email') != claims['email']):
            raise AuthorizationError('identity_invalid')
        claims.update({name:info[name] for name in ('email','email_verified','name') if name in info})
    # Retain only identity claims needed for account resolution. Neither raw ID
    # tokens nor optional access/refresh tokens cross this protocol boundary.
    return {name: claims.get(name) for name in ('iss', 'sub', 'email', 'email_verified', 'name', 'sid')}
