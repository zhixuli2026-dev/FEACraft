"""Atomic, browser-bound website login and revocable first-party sessions."""
import asyncio
from datetime import timedelta
import hmac
import re

from fastapi import HTTPException
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.exc import IntegrityError

from ..db import SessionLocal
from ..config import get_settings
from ..models import ExternalIdentity, User, WebsiteAuthSession, WebsiteOAuthTransaction, Workspace
from ..auth_models import now
from . import siwc_protocol as protocol
from .async_ops import finish_before_cancel

ACTIVE = ('pending', 'processing')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
EMAIL = TypeAdapter(EmailStr)
SESSION_SECONDS = 8 * 60 * 60


def browser_hash(raw):
    return protocol.digest(raw) if isinstance(raw, str) and TOKEN.fullmatch(raw) else None


def require_origin(connection, cfg=None):
    from . import oidc
    if cfg is not None:
        allowed = {cfg.public_origin}
    else:
        allowed = {entry.public_origin for entry in oidc.configured().values()}
        try:
            allowed.add(protocol.configuration().public_origin)
        except protocol.AuthorizationError:
            if origin := get_settings().siwc_public_origin.rstrip('/'):
                allowed.add(origin)
    if connection.headers.get('origin') not in allowed:
        raise HTTPException(403, '请求来源不匹配，请从本站重新操作')


def session_cookie(connection):
    from . import oidc
    raw = connection.cookies.get(protocol.SESSION_COOKIE)
    if raw is None and any(not entry.secure_cookies for entry in oidc.configured().values()):
        raw = connection.cookies.get(oidc.LOCAL_SESSION_COOKIE)
    return raw


async def expire(db):
    await db.execute(update(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.phase.in_(ACTIVE),
        WebsiteOAuthTransaction.expires_at <= now()).values(phase='expired', ended_at=now(), verifier_ciphertext=None,
            error_code='expired'))


async def expiry_loop(stop):
    while not stop.is_set():
        try:
            async with SessionLocal() as db:
                await expire(db)
                await db.commit()
        except Exception as error:
            # Never log transaction parameters or provider response content.
            print('[WARN] Website authorization expiry: ' + type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass


async def start(cfg, intent, user_id, previous_browser):
    state, browser, nonce, verifier = (protocol.random_value() for _ in range(4))
    url = await protocol.authorization_url(cfg, state, verifier, nonce)
    async with SessionLocal() as db:
        await expire(db)
        if intent == 'link':
            user = await db.get(User, user_id, populate_existing=True)
            if user is None or user.status != 'active':
                raise protocol.AuthorizationError('account_unavailable')
        if old_hash := browser_hash(previous_browser):
            await db.execute(update(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.browser_sha256 == old_hash,
                WebsiteOAuthTransaction.phase.in_(ACTIVE)).values(phase='cancelled', verifier_ciphertext=None,
                    ended_at=now(), error_code='replaced'))
        row = WebsiteOAuthTransaction(state_sha256=protocol.digest(state), browser_sha256=protocol.digest(browser),
            nonce_sha256=protocol.digest(nonce), verifier_ciphertext=protocol.vault().encrypt(verifier.encode()).decode(),
            config_sha256=cfg.fingerprint, intent=intent, link_user_id=user_id if intent == 'link' else None,
            expires_at=now() + timedelta(minutes=10))
        db.add(row)
        await finish_before_cancel(db.commit())
        return {'authorization_url': url, 'id': row.id, 'expires_at': row.expires_at.isoformat()}, browser


async def pending(browser):
    if (bound := browser_hash(browser)) is None:
        return None
    async with SessionLocal() as db:
        await expire(db)
        row = await db.scalar(select(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.browser_sha256 == bound)
                              .order_by(WebsiteOAuthTransaction.created_at.desc()).limit(1))
        await db.commit()
        return {'id': row.id, 'phase': row.phase, 'intent': row.intent, 'expires_at': row.expires_at.isoformat(),
                'error_code': row.error_code} if row else None


async def cancel(browser):
    if (bound := browser_hash(browser)) is not None:
        async with SessionLocal() as db:
            await db.execute(update(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.browser_sha256 == bound,
                WebsiteOAuthTransaction.phase.in_(ACTIVE)).values(phase='cancelled', verifier_ciphertext=None,
                    ended_at=now(), error_code='cancelled'))
            await finish_before_cancel(db.commit())


async def fail(identifier, code):
    async with SessionLocal() as db:
        await db.execute(update(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.id == identifier,
            WebsiteOAuthTransaction.phase == 'processing').values(phase='failed', verifier_ciphertext=None,
                ended_at=now(), error_code=code))
        await finish_before_cancel(db.commit())


async def claim(cfg, state, browser):
    bound = browser_hash(browser)
    if bound is None or browser_hash(state) is None:
        raise protocol.AuthorizationError('state_invalid')
    async with SessionLocal() as db:
        await expire(db)
        row = await db.scalar(select(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.state_sha256 == protocol.digest(state),
            WebsiteOAuthTransaction.browser_sha256 == bound))
        if row is None or row.phase != 'pending':
            await db.commit()
            raise protocol.AuthorizationError('state_invalid')
        if not hmac.compare_digest(row.config_sha256, cfg.fingerprint):
            await db.execute(update(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.id == row.id,
                WebsiteOAuthTransaction.phase == 'pending').values(phase='failed', verifier_ciphertext=None,
                    ended_at=now(), error_code='configuration_changed'))
            await db.commit()
            raise protocol.AuthorizationError('configuration_changed')
        result = await db.execute(update(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.id == row.id,
            WebsiteOAuthTransaction.phase == 'pending', WebsiteOAuthTransaction.expires_at > now()).values(phase='processing')
            .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            await db.rollback()
            raise protocol.AuthorizationError('state_invalid')
        await finish_before_cancel(db.commit())
        return row


def identity_email(identity):
    email = identity.get('email')
    if email is None:
        return None
    try:
        if not isinstance(email, str) or len(email) > 255:
            raise ValueError()
        return str(EMAIL.validate_python(email)).lower()
    except ValueError as error:
        raise protocol.AuthorizationError('identity_invalid') from error


async def complete(cfg, transaction, identity, *, configuration=protocol.configuration):
    token = protocol.random_value()
    email = identity_email(identity)
    async with SessionLocal() as db:
        # CAS takes a write lock before resolving identity/account uniqueness.
        claimed = await db.execute(update(WebsiteOAuthTransaction).where(WebsiteOAuthTransaction.id == transaction.id,
            WebsiteOAuthTransaction.phase == 'processing', WebsiteOAuthTransaction.expires_at > now(),
            WebsiteOAuthTransaction.config_sha256 == cfg.fingerprint).values(phase='completed', ended_at=now(), verifier_ciphertext=None))
        if claimed.rowcount != 1 or cfg.fingerprint != configuration().fingerprint:
            raise protocol.AuthorizationError('transaction_unavailable')
        # All account/identity mutations lock the user before the identity.
        # Otherwise login's session FK check can deadlock with unlink's user lock.
        candidate = (transaction.link_user_id if transaction.intent == 'link' else
            await db.scalar(select(ExternalIdentity.user_id).where(ExternalIdentity.issuer == cfg.issuer,
                ExternalIdentity.client_id == cfg.client_id, ExternalIdentity.subject == identity['sub'],
                ExternalIdentity.revoked_at.is_(None))))
        user = (await db.get(User, candidate, populate_existing=True, with_for_update=True)
                if candidate is not None else None)
        linked = await db.scalar(select(ExternalIdentity).where(ExternalIdentity.issuer == cfg.issuer,
            ExternalIdentity.client_id == cfg.client_id, ExternalIdentity.subject == identity['sub']).with_for_update())
        if transaction.intent == 'link':
            if linked is not None and linked.revoked_at is None and linked.user_id != transaction.link_user_id:
                raise protocol.AuthorizationError('identity_already_linked')
        elif linked is not None and linked.revoked_at is None:
            if user is None or user.id != linked.user_id:
                raise protocol.AuthorizationError('account_conflict')
        else:
            if linked is not None or (email is not None and await db.scalar(select(User.id).where(func.lower(User.email) == email).limit(1))):
                raise protocol.AuthorizationError('account_link_required')
            if email is None:
                raise protocol.AuthorizationError('account_setup_required')
            name = identity.get('name')
            name = name.strip()[:120] if isinstance(name, str) else email.split('@')[0][:120]
            # Existing registration permits unverified contact email. Record
            # its verification separately; it never proves account ownership.
            user = User(email=email, name=name or email.split('@')[0][:120], password_hash='external-only')
            db.add(user)
            await db.flush()
            db.add(Workspace(user_id=user.id, name='Default'))
            from .quota import ensure_user_org
            await ensure_user_org(db, user)
        if user is None or user.status != 'active':
            raise protocol.AuthorizationError('account_unavailable')
        if linked is None:
            linked = ExternalIdentity(user_id=user.id, issuer=cfg.issuer, client_id=cfg.client_id, subject=identity['sub'])
            db.add(linked)
            await db.flush()
        elif linked.revoked_at is not None:
            await db.execute(update(WebsiteAuthSession).where(WebsiteAuthSession.identity_id == linked.id,
                WebsiteAuthSession.revoked_at.is_(None)).values(revoked_at=now()))
            linked.user_id, linked.revoked_at = user.id, None
        linked.display_email, linked.email_verified = email, identity.get('email_verified') is True
        db.add(WebsiteAuthSession(token_sha256=protocol.digest(token), user_id=user.id, identity_id=linked.id,
            provider_sid=identity.get('sid'), expires_at=now() + timedelta(seconds=SESSION_SECONDS)))
        await finish_before_cancel(db.commit())
    return token


async def callback(cfg, state, browser, code, provider_error, *, exchange=None, configuration=None):
    transaction = await claim(cfg, state, browser)
    try:
        if provider_error:
            raise protocol.AuthorizationError('access_denied' if provider_error == 'access_denied' else 'provider_denied')
        if not isinstance(code, str) or not 1 <= len(code) <= 4096 or any(ord(c) < 33 for c in code):
            raise protocol.AuthorizationError('callback_invalid')
        verifier = protocol.vault().decrypt(transaction.verifier_ciphertext.encode()).decode()
        identity = await (exchange or protocol.exchange)(cfg, code, verifier, transaction.nonce_sha256)
        return await complete(cfg, transaction, identity, configuration=configuration or protocol.configuration)
    except BaseException as error:
        reason = error.code if isinstance(error, protocol.AuthorizationError) else 'account_conflict' if isinstance(error, IntegrityError) else 'authorization_failed'
        await finish_before_cancel(fail(transaction.id, reason))
        if isinstance(error, protocol.AuthorizationError):
            raise
        if isinstance(error, Exception):
            raise protocol.AuthorizationError(reason) from error
        raise


def enabled_identities():
    from . import oidc
    pairs={(entry.issuer,entry.client_id) for entry in oidc.configured().values()}
    try:
        cfg = protocol.configuration()
        pairs.add((cfg.issuer,cfg.client_id))
    except protocol.AuthorizationError:
        pass
    return pairs


async def cookie_user(db, raw):
    if (hashed := browser_hash(raw)) is None or not (pairs := enabled_identities()):
        return None
    return await db.scalar(select(User).join(WebsiteAuthSession, WebsiteAuthSession.user_id == User.id)
        .join(ExternalIdentity, ExternalIdentity.id == WebsiteAuthSession.identity_id).where(
            WebsiteAuthSession.token_sha256 == hashed, WebsiteAuthSession.revoked_at.is_(None), WebsiteAuthSession.expires_at > now(),
            ExternalIdentity.user_id == User.id, ExternalIdentity.revoked_at.is_(None), User.status == 'active',
            tuple_(ExternalIdentity.issuer,ExternalIdentity.client_id).in_(sorted(pairs))).execution_options(populate_existing=True))


async def logout(raw):
    if (hashed := browser_hash(raw)) is not None:
        async with SessionLocal() as db:
            identity=await db.scalar(update(WebsiteAuthSession).where(WebsiteAuthSession.token_sha256==hashed,
                WebsiteAuthSession.revoked_at.is_(None)).values(revoked_at=now()).returning(WebsiteAuthSession.identity_id))
            pair=(await db.execute(select(ExternalIdentity.issuer,ExternalIdentity.client_id)
                .where(ExternalIdentity.id==identity))).one_or_none() if identity is not None else None
            await finish_before_cancel(db.commit())
            return tuple(pair) if pair is not None else None
    return None


async def unlink(identifier, user_id, *, organization=False):
    async with SessionLocal() as db:
        user = await db.get(User, user_id, populate_existing=True, with_for_update=True)
        row = await db.get(ExternalIdentity, identifier, populate_existing=True, with_for_update=True)
        if (user is None or user.status != 'active' or row is None or row.user_id != user_id or row.revoked_at is not None
                or (row.issuer!=protocol.ISSUER)!=organization):
            raise HTTPException(404, '登录关联不存在')
        count = await db.scalar(select(func.count()).select_from(ExternalIdentity).where(
            ExternalIdentity.user_id == user_id, ExternalIdentity.revoked_at.is_(None), ExternalIdentity.id!=identifier,
            tuple_(ExternalIdentity.issuer,ExternalIdentity.client_id).in_(sorted(enabled_identities()))))
        if not user.password_hash.startswith('pbkdf2$') and not count:
            raise HTTPException(409, '这是唯一的登录方式，请先添加其他登录方式')
        row.revoked_at = now()
        await db.execute(update(WebsiteAuthSession).where(WebsiteAuthSession.identity_id == row.id,
            WebsiteAuthSession.revoked_at.is_(None)).values(revoked_at=now()))
        await finish_before_cancel(db.commit())
