"""Configured organization identity providers and the shared website OIDC exchange."""
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit

from ..config import get_settings
from . import siwc_protocol as protocol


STATE_COOKIE = '__Host-vf_oidc_state'
LOCAL_STATE_COOKIE = 'vf_oidc_local_state'
LOCAL_SESSION_COOKIE = 'vf_oidc_local_session'
_PROVIDER = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')


@dataclass(frozen=True)
class Configuration(protocol.Configuration):
    provider_id: str = 'default'
    name: str = '组织 OIDC'
    callback_url: str = ''
    endpoint_origins: tuple[str, ...] = ()
    rp_logout: bool = False

    @property
    def redirect_uri(self):
        return self.callback_url or f'{self.public_origin}/api/v1/auth/oidc/{self.provider_id}/callback'

    @property
    def fingerprint(self):
        value = [self.provider_id, self.redirect_uri, self.endpoint_origins, super().fingerprint]
        return hmac.new(get_settings().jwt_secret.encode(),
            json.dumps(value,separators=(',',':')).encode(),hashlib.sha256).hexdigest()

    @property
    def state_cookie(self):
        return STATE_COOKIE if self.secure_cookies else LOCAL_STATE_COOKIE

    @property
    def session_cookie(self):
        return protocol.SESSION_COOKIE if self.secure_cookies else LOCAL_SESSION_COOKIE


def _url(value, *, path=False):
    if not isinstance(value,str) or not 1<=len(value)<=300 or any(ord(c)<33 for c in value):
        raise protocol.AuthorizationError('invalid_configuration')
    try:
        url = urlsplit(value)
        local = url.hostname in {'127.0.0.1','::1','localhost'}
        if (not url.hostname or not (url.scheme=='https' or url.scheme=='http' and local)
                or url.port==0 or url.username is not None or url.query or url.fragment
                or '\\' in value or (url.path and not path)):
            raise ValueError
    except ValueError:
        raise protocol.AuthorizationError('invalid_configuration') from None
    return url


def _build(provider_id, item, origin):
    if not isinstance(provider_id,str) or not _PROVIDER.fullmatch(provider_id) or not isinstance(item,dict):
        raise protocol.AuthorizationError('invalid_configuration')
    issuer, client_id = item.get('issuer'), item.get('client_id')
    secret, name = item.get('client_secret',''), item.get('name','组织 OIDC')
    method = item.get('token_auth_method','client_secret_basic' if secret else 'none')
    callback = item.get('redirect_uri','')
    endpoints = item.get('endpoint_origins',[])
    rp_logout = item.get('rp_logout',False)
    _url(issuer,path=True)
    allowed_legacy = provider_id=='default' and callback==origin+'/oidc/callback'
    expected = f'{origin}/api/v1/auth/oidc/{provider_id}/callback'
    if (issuer.rstrip('/')==protocol.ISSUER
            or not isinstance(client_id,str) or not 1<=len(client_id)<=200
            or any(not 33<=ord(c)<=126 for c in client_id)
            or not isinstance(secret,str) or len(secret)>4096
            or not isinstance(name,str) or not 1<=len(name.strip())<=80
            or not isinstance(method,str) or method not in {'none','client_secret_basic','client_secret_post'}
            or bool(secret)!=(method!='none')
            or callback and callback!=expected and not allowed_legacy
            or not isinstance(endpoints,list) or len(endpoints)>8 or type(rp_logout) is not bool):
        raise protocol.AuthorizationError('invalid_configuration')
    return Configuration(client_id=client_id, public_origin=origin, issuer=issuer.rstrip('/'),
        token_auth_method=method, client_secret=secret, secure_cookies=origin.startswith('https:'),
        provider_id=provider_id, name=name.strip(), callback_url=callback, rp_logout=rp_logout,
        endpoint_origins=tuple(f'{x.scheme}://{x.netloc}' for x in (_url(value) for value in endpoints)))


def configurations():
    settings=get_settings()
    entries=dict(settings.oidc_providers)
    if settings.oidc_provider_file:
        try:
            with Path(settings.oidc_provider_file).open('rb') as stream:
                payload=stream.read(65537)
            if len(payload)>65536 or entries:
                raise ValueError
            entries=json.loads(payload)
            if not isinstance(entries,dict):
                raise ValueError
        except (OSError,ValueError):
            raise protocol.AuthorizationError('invalid_configuration') from None
    origin=settings.oidc_public_origin.rstrip('/')
    if settings.oidc_issuer or settings.oidc_client_id:
        if 'default' in entries:
            raise protocol.AuthorizationError('invalid_configuration')
        if not origin:
            redirect=_url(settings.oidc_redirect_uri or 'http://localhost:5173/oidc/callback',path=True)
            origin=f'{redirect.scheme}://{redirect.netloc}'
        entries['default']={'issuer':settings.oidc_issuer,'client_id':settings.oidc_client_id,
            'client_secret':settings.oidc_client_secret,'redirect_uri':settings.oidc_redirect_uri
                or origin+'/oidc/callback',
            'token_auth_method':'client_secret_post' if settings.oidc_client_secret else 'none'}
    if not entries:
        return {}
    _url(origin)
    if (len(entries)>12 or len(settings.jwt_secret)<32 or settings.jwt_secret=='dev-only-secret-change-me'
            or settings.siwc_public_origin and settings.siwc_public_origin.rstrip('/')!=origin):
        raise protocol.AuthorizationError('invalid_configuration')
    choices={name:_build(name,item,origin) for name,item in entries.items()}
    if len({(cfg.issuer,cfg.client_id) for cfg in choices.values()})!=len(choices):
        raise protocol.AuthorizationError('invalid_configuration')
    return choices


def configured():
    try:
        return configurations()
    except protocol.AuthorizationError:
        return {}


def configuration(provider_id):
    cfg=configurations().get(provider_id)
    if cfg is None:
        raise protocol.AuthorizationError('not_configured')
    return cfg


async def exchange(cfg, code, verifier, nonce_sha256):
    identity=await protocol.exchange(cfg,code,verifier,nonce_sha256,userinfo=True)
    if not identity['email'] or identity['email_verified'] is not True:
        raise protocol.AuthorizationError('identity_email_unverified')
    return identity


async def logout_url(identity):
    for cfg in configured().values():
        if cfg.rp_logout and (cfg.issuer,cfg.client_id)==identity:
            try:
                metadata=await protocol.discovery(cfg)
                endpoint=protocol.trusted_endpoint(metadata.get('end_session_endpoint'),cfg)
                return endpoint+'?'+urlencode({'client_id':cfg.client_id,
                    'post_logout_redirect_uri':cfg.public_origin+'/'})
            except (protocol.AuthorizationError,ValueError):
                return None
    return None
