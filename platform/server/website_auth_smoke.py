"""Real HTTPS product routes with a signed, local OpenID provider and cookies."""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from sqlalchemy.engine import make_url

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from app.services.siwc_protocol import challenge, SESSION_COOKIE, STATE_COOKIE

CLIENT = 'oaiapp_local_fixture'
SECRET = 'local secret:+&/with?punctuation'


class Provider(BaseHTTPRequestHandler):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alternate = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    mode = 'normal'
    subject = 'local-user'
    email = 'website-user@example.com'
    codes = {}
    exchanges = []
    jwks_calls = 0
    entered, release = threading.Event(), threading.Event()

    def log_message(self, *_args):
        pass

    def output(self, value, cookie=None):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if cookie: self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        issuer = f'http://127.0.0.1:{self.server.server_port}'
        if parsed.path == '/viz-probe':
            return self.output({'cookie': 'Cookie' in self.headers, 'authorization': 'Authorization' in self.headers,
                'token_query': 'token' in parse_qs(parsed.query)}, cookie='__Host-vf_session=upstream-test; Secure; HttpOnly; Path=/')
        if parsed.path == '/.well-known/openid-configuration':
            return self.output({'issuer': issuer, 'authorization_endpoint': issuer + '/authorize',
                'token_endpoint': issuer + '/token', 'jwks_uri': issuer + '/jwks', 'response_types_supported': ['code'],
                'code_challenge_methods_supported': ['S256'], 'id_token_signing_alg_values_supported': ['RS256']})
        if parsed.path == '/jwks':
            type(self).jwks_calls += 1
            keys = []
            for kid, key in [('current', self.key), ('rotated', self.alternate)]:
                jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
                keys.append({**jwk, 'kid': kid, 'use': 'sig', 'alg': 'RS256'})
            return self.output({'keys': keys if self.mode == 'rotated_key' else keys[:1]})
        assert parsed.path == '/authorize'
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        assert query['client_id'] == CLIENT and query['scope'] == 'openid profile email'
        assert query['response_type'] == 'code' and query['code_challenge_method'] == 'S256'
        code = uuid.uuid4().hex
        self.codes[code] = (query, self.mode, self.subject, self.email)
        self.send_response(303)
        self.send_header('Location', query['redirect_uri'] + '?' + urlencode({'code': code, 'state': query['state']}))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        assert self.path == '/token'
        form = {key: value[0] for key, value in parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode()).items()}
        query, mode, subject, email = self.codes.pop(form['code'])
        assert set(form) == {'grant_type', 'code', 'client_id', 'redirect_uri', 'code_verifier'}
        assert form['grant_type'] == 'authorization_code' and form['client_id'] == CLIENT
        assert form['redirect_uri'] == query['redirect_uri'] and challenge(form['code_verifier']) == query['code_challenge']
        basic = 'Basic ' + base64.b64encode((quote_plus(CLIENT) + ':' + quote_plus(SECRET)).encode()).decode()
        auth = self.headers.get('Authorization')
        assert auth in (None, basic)
        self.exchanges.append({'pkce_verified': True, 'method': 'client_secret_basic' if auth else 'none', 'mode': mode})
        if mode == 'held':
            self.entered.set()
            assert self.release.wait(20)
        now = int(time.time())
        claims = {'iss': f'http://127.0.0.1:{self.server.server_port}', 'aud': [CLIENT], 'sub': subject,
            'iat': now, 'exp': now + 300, 'nonce': query['nonce'], 'name': 'Local identity',
            'email': email, 'email_verified': False, 'sid': 'provider-session-1'}
        if mode == 'missing_email':
            claims.pop('email')
        if mode == 'wrong_nonce': claims['nonce'] = 'wrong'
        if mode == 'wrong_issuer': claims['iss'] = 'https://untrusted.example.test'
        if mode == 'wrong_audience': claims['aud'] = ['another-client']
        if mode == 'expired': claims.update(iat=now - 600, exp=now - 10)
        if mode == 'future_issued': claims.update(iat=now + 60, exp=now + 300)
        if mode == 'missing_subject': claims.pop('sub')
        if mode == 'bad_sid': claims['sid'] = 3
        if mode == 'multiple_audience': claims['aud'] = [CLIENT, 'another-client']
        if mode == 'bad_azp': claims['azp'] = 'another-client'
        kid = 'rotated' if mode == 'rotated_key' else 'unknown' if mode == 'unknown_key' else 'current'
        key = self.alternate if mode in {'bad_signature', 'rotated_key'} else self.key
        token = jwt.encode(claims, 'untrusted-hmac-key-for-negative-test-only' if mode == 'bad_algorithm' else key,
            algorithm='HS256' if mode == 'bad_algorithm' else 'RS256', headers={'kid': kid})
        # Identity-only response deliberately has no access or refresh token.
        self.output({'id_token': token})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Browser:
    def __init__(self, api):
        self.api, self.jar = api, CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=api.tls_context), urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, *, expect=200, origin=True, bearer=None):
        headers = {'Origin': self.api.base} if origin and method not in {'GET', 'HEAD'} else {}
        if bearer: headers['Authorization'] = 'Bearer ' + bearer
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        url = path if '://' in path else self.api.base + '/api/v1' + path
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try: response = self.opener.open(request, timeout=25)
        except urllib.error.HTTPError as error: response = error
        with response:
            data = response.read()
            assert response.code == expect, (method, urlsplit(url).path, response.code, data[:300])
            return (json.loads(data) if data else None), response.headers

    def flow(self, *, intent='login', bearer=None):
        started, headers = self.call('POST', '/auth/chatgpt/start', {'intent': intent}, bearer=bearer)
        cookie = headers['Set-Cookie']
        assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie and 'Path=/' in cookie
        _, redirect = self.call('GET', started['authorization_url'], expect=303)
        return started, redirect['Location']

    def callback(self, url, expected='complete'):
        _, headers = self.call('GET', url, expect=303)
        assert headers['Location'] == self.api.base + '/?chatgpt=' + expected, headers['Location']
        assert headers['Cache-Control'] == 'no-store'
        return headers


FIXTURE = '''from datetime import timedelta
from fastapi import Body
from sqlalchemy import select,func,update
from app.main import app
from app.db import SessionLocal
from app.auth_models import now
from app.models import WebsiteOAuthTransaction,WebsiteAuthSession,ExternalIdentity,User
from app.services import siwc_protocol as protocol
config=protocol.Configuration(CLIENT,ORIGIN,issuer=ISSUER)
protocol.configuration=lambda:config
@app.get('/api/v1/fixture/state/{identifier}')
async def state(identifier:str):
    async with SessionLocal() as db:
        row=await db.get(WebsiteOAuthTransaction,identifier)
        return {'phase':row.phase,'verifier_cleared':row.verifier_ciphertext is None,'error_code':row.error_code,
            'sessions':await db.scalar(select(func.count()).select_from(WebsiteAuthSession)),
            'identities':await db.scalar(select(func.count()).select_from(ExternalIdentity))}
@app.post('/api/v1/fixture/expire/{identifier}')
async def expire(identifier:str):
    async with SessionLocal() as db:
        row=await db.get(WebsiteOAuthTransaction,identifier)
        row.created_at=now()-timedelta(minutes=20);row.expires_at=now()-timedelta(seconds=10)
        await db.commit();return {'ok':True}
@app.post('/api/v1/fixture/confidential/{enabled}')
async def confidential(enabled:int):
    global config
    config=protocol.Configuration(CLIENT,ORIGIN,issuer=ISSUER,token_auth_method='client_secret_basic' if enabled else 'none',client_secret=SECRET if enabled else '')
    return {'ok':True}
@app.post('/api/v1/fixture/disable/{uid}')
async def disable(uid:int):
    async with SessionLocal() as db:
        user=await db.get(User,uid);user.status='disabled';await db.commit();return {'ok':True}
@app.post('/api/v1/fixture/viz')
async def fixture_viz(body:dict=Body(...)):
    from types import SimpleNamespace
    import time
    from app.routers.viz import _sessions
    async def verify():pass
    async def close(*args):_sessions.pop(987654,None)
    _sessions[987654]=SimpleNamespace(user_id=body['user_id'],case_id=body['case_id'],port=body['port'],
        alive=True,_close_task=None,last_activity=time.time(),owned=SimpleNamespace(proc=SimpleNamespace(returncode=None)),
        verify_ready=verify,touch=lambda:None,close=close)
    return {'ok':True}
@app.delete('/api/v1/fixture/viz')
async def remove_fixture_viz():
    from app.routers.viz import _sessions
    _sessions.pop(987654,None)
    return {'ok':True}
'''


def launch_same_environment(api, port, log):
    return subprocess.Popen([sys.executable, '-m', 'uvicorn', api.app, '--app-dir', str(SERVER),
        '--host', '127.0.0.1', '--port', str(port), *api.tls_args], cwd=api.root, env=api.env,
        stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def ready(api, base, proc):
    for _ in range(150):
        assert proc.poll() is None, 'owned test API exited during startup'
        try:
            with api.opener.open(base + '/api/health', timeout=1) as response:
                if json.load(response)['ok']: return
        except (OSError, ValueError): time.sleep(.1)
    raise TimeoutError('owned test API startup timed out')


def restart(api):
    api.proc.terminate(); api.proc.wait(timeout=10)
    api.log.close(); api.log = (api.root / 'api.log').open('a', encoding='utf8')
    api.proc = launch_same_environment(api, api.port, api.log)
    ready(api, api.base, api.proc)


@contextmanager
def peer(api):
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'https://127.0.0.1:{port}'
    with (api.root / 'peer.log').open('w', encoding='utf8') as log:
        proc = launch_same_environment(api, port, log)
        try:
            ready(api, base, proc)
            yield base
        finally:
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)


def cookie_websocket(browser, case_id):
    import aiohttp
    async def check():
        raw = '; '.join(cookie.name + '=' + cookie.value for cookie in browser.jar)
        url = browser.api.base.replace('https:', 'wss:') + f'/ws/events?case_id={case_id}'
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, ssl=browser.api.tls_context,
                    headers={'Cookie': raw, 'Origin': browser.api.base}) as ws:
                message = await ws.receive_json(timeout=5)
                assert message['type'] == 'ready' and message['case_id'] == case_id
            for origin in (None, 'https://foreign.example.test'):
                try:
                    async with session.ws_connect(url, ssl=browser.api.tls_context,
                            headers={'Cookie': raw, **({'Origin': origin} if origin else {})}):
                        raise AssertionError('foreign origin accepted')
                except aiohttp.WSServerHandshakeError as error:
                    assert error.status == 403
    asyncio.run(check())


def run_database(url, folder):
    folder.mkdir(parents=True)
    if url is not None: alembic(url, folder, 'upgrade', 'head')
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    api = IsolatedServer('website-auth', app='auth_fixture:app', database_url=url, tls=True, access_log=True)
    prefix = 'CLIENT=' + repr(CLIENT) + '\nSECRET=' + repr(SECRET) + '\nORIGIN=' + repr(api.base) + '\nISSUER=' + repr(f'http://127.0.0.1:{provider.server_port}') + '\n'
    (api.root / 'auth_fixture.py').write_text(prefix + FIXTURE, encoding='utf8')
    checks, receipts, callback_urls = [], [], []
    Provider.mode, Provider.subject, Provider.email = 'normal', 'local-user', 'website-user@example.com'
    try:
        with api:
            browser = Browser(api)
            browser.call('POST', '/auth/chatgpt/start', {}, origin=False, expect=403)
            checks.append('login_start_requires_same_origin')
            first, callback = browser.flow()
            callback_urls.append(callback)
            before = len(Provider.exchanges)
            stranger = Browser(api)
            stranger.callback(callback, 'state_invalid')
            assert len(Provider.exchanges) == before
            assert api.call('GET', '/fixture/state/' + first['id'])['phase'] == 'pending'
            checks.append('foreign_browser_cannot_consume_login_transaction')
            saved_cookie = copy.deepcopy(next(c for c in browser.jar if c.name == STATE_COOKIE))
            browser.callback(callback)
            user = browser.call('GET', '/auth/me')[0]
            assert user['email'] == Provider.email
            connections = browser.call('GET', '/auth/chatgpt/connections')[0]
            assert len(connections) == 1 and connections[0]['email_verified'] is False
            state = api.call('GET', '/fixture/state/' + first['id'])
            assert state['phase'] == 'completed' and state['verifier_cleared'] and state['sessions'] == 1
            receipts.append(state)
            checks.append('signed_identity_only_login_creates_cookie_session_and_external_identity')
            browser.jar.set_cookie(saved_cookie)
            browser.callback(callback, 'state_invalid')
            assert len(Provider.exchanges) == before + 1
            checks.append('consumed_callback_cannot_repeat_token_exchange')
            browser.call('POST', '/workspaces', {'name': 'CSRF denied'}, origin=False, expect=403)
            browser.call('POST', '/workspaces', {'name': 'Cookie workspace'})
            browser.call('DELETE', '/auth/chatgpt/connections/' + connections[0]['id'], expect=409)
            checks.append('cookie_write_csrf_and_last_signin_method_protection')
            stolen = copy.deepcopy(next(c for c in browser.jar if c.name == SESSION_COOKIE))
            browser.call('POST', '/auth/logout', {})
            browser.jar.set_cookie(stolen)
            browser.call('GET', '/auth/me', expect=401)
            checks.append('logout_revokes_server_session_even_if_cookie_is_replayed')
            browser = Browser(api)
            started, callback = browser.flow()
            browser.callback(callback)
            assert browser.call('GET', '/auth/me')[0]['id'] == user['id']
            checks.append('existing_issuer_client_subject_reuses_local_account')
            workspace = browser.call('GET', '/workspaces')[0][0]
            case = browser.call('POST', f"/workspaces/{workspace['id']}/cases", {'name': 'Cookie session case'})[0]
            cookie_websocket(browser, case['id'])
            checks.append('cookie_event_websocket_authorizes_owner_and_rejects_foreign_or_missing_origin')
            api.call('POST', '/fixture/viz', {'user_id': user['id'], 'case_id': case['id'], 'port': provider.server_port})
            try:
                stale_viz = copy.deepcopy(next(c for c in browser.jar if c.name == SESSION_COOKIE))
                stale_viz.name, stale_viz.value, stale_viz.path = 'vf_viz_987654', 'expired-legacy-token', '/viz-proxy/987654/'
                browser.jar.set_cookie(stale_viz)
                result, headers = browser.call('GET', api.base + '/viz-proxy/987654/viz-probe?token=', bearer='fixture-header-secret')
                assert result == {'cookie': False, 'authorization': False, 'token_query': False}
                assert not headers.get('Set-Cookie')
                assert browser.call('GET', '/auth/me')[0]['id'] == user['id']
                Browser(api).call('GET', api.base + '/viz-proxy/987654/viz-probe', expect=401)
                checks.append('cookie_viz_gateway_strips_credentials_and_upstream_cookies_and_ignores_stale_view_token')
            finally: api.call('DELETE', '/fixture/viz')
            browser.call('GET', '/auth/me', bearer='invalid-legacy-token', expect=401)
            checks.append('invalid_explicit_bearer_never_falls_back_to_cookie')
            persistent = Browser(api)
            started, callback = persistent.flow()
            restart(api)
            persistent.callback(callback)
            assert persistent.call('GET', '/auth/me')[0]['id'] == user['id']
            assert browser.call('GET', '/auth/me')[0]['id'] == user['id']
            checks.append('pending_pkce_and_existing_session_survive_api_restart')
            with peer(api) as secondary:
                racing = Browser(api)
                Provider.mode = 'held'; Provider.entered.clear(); Provider.release.clear()
                started, callback = racing.flow()
                duplicate = Browser(api)
                for cookie in racing.jar: duplicate.jar.set_cookie(copy.deepcopy(cookie))
                before = len(Provider.exchanges)
                with ThreadPoolExecutor(1) as pool:
                    future = pool.submit(racing.callback, callback)
                    assert Provider.entered.wait(10)
                    duplicate.callback(callback.replace(api.base, secondary), 'state_invalid')
                    Provider.release.set(); future.result(timeout=15)
                assert len(Provider.exchanges) == before + 1
                assert racing.call('GET', secondary + '/api/v1/auth/me')[0]['id'] == user['id']
                checks.append('two_api_callback_claim_exchanges_once_and_shares_cookie_session')
                saved = copy.deepcopy(next(c for c in racing.jar if c.name == SESSION_COOKIE))
                racing.call('POST', secondary + '/api/v1/auth/logout', {})
                racing.jar.set_cookie(saved)
                racing.call('GET', '/auth/me', expect=401)
                checks.append('logout_from_other_api_revokes_shared_session')
                Provider.mode = 'normal'
            for mode in ('bad_signature', 'wrong_nonce', 'wrong_issuer', 'wrong_audience', 'expired', 'future_issued',
                         'missing_subject', 'bad_sid', 'multiple_audience', 'bad_azp', 'unknown_key', 'bad_algorithm'):
                Provider.mode = mode
                invalid = Browser(api)
                started, callback = invalid.flow()
                invalid.callback(callback, 'identity_invalid')
                state = api.call('GET', '/fixture/state/' + started['id'])
                assert state['phase'] == 'failed' and state['verifier_cleared'], state
                invalid.call('GET', '/auth/me', expect=401)
                checks.append('reject_' + mode)
            Provider.mode = 'rotated_key'
            rotated = Browser(api)
            before_keys = Provider.jwks_calls
            started, callback = rotated.flow()
            rotated.callback(callback)
            assert Provider.jwks_calls > before_keys
            checks.append('unknown_rotated_kid_refreshes_jwks')
            Provider.mode = 'normal'
            cancelled = Browser(api)
            started, callback = cancelled.flow()
            cancelled.call('POST', '/auth/chatgpt/cancel', {})
            before = len(Provider.exchanges)
            cancelled.callback(callback, 'state_invalid')
            assert len(Provider.exchanges) == before
            assert api.call('GET', '/fixture/state/' + started['id'])['verifier_cleared']
            checks.append('cancelled_callback_does_not_exchange_code')
            expired = Browser(api)
            started, callback = expired.flow()
            api.call('POST', '/fixture/expire/' + started['id'])
            expired.callback(callback, 'state_invalid')
            assert api.call('GET', '/fixture/state/' + started['id'])['phase'] == 'expired'
            checks.append('expired_durable_transaction_clears_verifier')
            declined = Browser(api)
            started, callback = declined.flow()
            state_query = parse_qs(urlsplit(callback).query)['state'][0]
            callback = callback.split('?')[0] + '?' + urlencode({'state': state_query, 'error': 'access_denied'})
            declined.callback(callback, 'access_denied')
            assert api.call('GET', '/fixture/state/' + started['id'])['verifier_cleared']
            checks.append('provider_denial_clears_temporary_authorization')
            pending = Browser(api)
            started, callback = pending.flow()
            api.call('POST', '/fixture/confidential/1')
            pending.callback(callback, 'configuration_changed')
            confidential = Browser(api)
            started, callback = confidential.flow()
            confidential.callback(callback)
            assert Provider.exchanges[-1]['method'] == 'client_secret_basic'
            checks.append('registered_confidential_method_uses_encoded_basic_header_only')
            api.call('POST', '/fixture/confidential/0')
            Provider.mode = 'held'
            Provider.entered.clear(); Provider.release.clear()
            racing = Browser(api)
            started, callback = racing.flow()
            copy_browser = Browser(api)
            for cookie in racing.jar: copy_browser.jar.set_cookie(copy.deepcopy(cookie))
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(racing.callback, callback, 'transaction_unavailable')
                assert Provider.entered.wait(10)
                copy_browser.call('POST', '/auth/chatgpt/cancel', {})
                Provider.release.set()
                future.result(timeout=15)
            racing.call('GET', '/auth/me', expect=401)
            assert api.call('GET', '/fixture/state/' + started['id'])['phase'] == 'cancelled'
            checks.append('cancel_during_token_exchange_prevents_session_commit')
            Provider.mode = 'normal'
            Provider.email = 'existing-local@example.com'
            registration = api.call('POST', '/auth/register', {'email': Provider.email, 'password': 'local-test-password', 'name': 'Existing'})
            Provider.subject = 'link-subject'
            linking = Browser(api)
            started, callback = linking.flow()
            linking.callback(callback, 'account_link_required')
            linking.call('GET', '/auth/me', expect=401)
            started, callback = linking.flow(intent='link', bearer=registration['token'])
            linking.callback(callback)
            assert linking.call('GET', '/auth/me')[0]['id'] == registration['user']['id']
            other = api.call('POST', '/auth/register', {'email': 'other-local@example.com', 'password': 'local-test-password', 'name': 'Other'})
            takeover = Browser(api)
            _, callback = takeover.flow(intent='link', bearer=other['token'])
            takeover.callback(callback, 'identity_already_linked')
            checks.append('linked_identity_cannot_be_moved_to_another_active_account')
            connection = linking.call('GET', '/auth/chatgpt/connections')[0][0]
            Provider.mode = 'held'; Provider.entered.clear(); Provider.release.clear()
            late = Browser(api)
            _, callback = late.flow()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(late.callback, callback, 'account_link_required')
                assert Provider.entered.wait(10)
                linking.call('DELETE', '/auth/chatgpt/connections/' + connection['id'])
                Provider.release.set(); future.result(timeout=15)
            late.call('GET', '/auth/me', expect=401)
            checks.append('unlink_during_login_exchange_cannot_resurrect_identity_or_session')
            linking.call('GET', '/auth/me', expect=401)
            checks.append('email_collision_requires_authenticated_link_and_unlink_revokes_sessions')
            Provider.mode, Provider.subject = 'missing_email', 'missing-email-subject'
            no_email = Browser(api)
            started, callback = no_email.flow()
            no_email.callback(callback, 'account_setup_required')
            checks.append('new_identity_without_contact_email_requires_account_setup')
            Provider.mode, Provider.subject, Provider.email = 'normal', 'local-user', 'website-user@example.com'
            api.call('POST', '/fixture/disable/' + str(user['id']))
            browser.call('GET', '/auth/me', expect=401)
            disabled = Browser(api)
            started, callback = disabled.flow()
            disabled.callback(callback, 'account_unavailable')
            checks.append('disabled_user_cannot_reuse_or_create_website_session')
        log = (api.root / 'api.log').read_text(encoding='utf8')
        for callback in callback_urls:
            for value in parse_qs(urlsplit(callback).query).values(): assert value[0] not in log
        assert SECRET not in log and 'code_verifier' not in log
        checks.append('callback_codes_state_and_client_secret_absent_from_access_log')
        result = {'passed': True, 'count': len(checks), 'checks': checks, 'https_product_api': True,
            'signed_local_idp': True, 'real_openai_client': False, 'evidence': str(api.root)}
        (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        return result
    finally:
        Provider.release.set()
        provider.shutdown(); provider.server_close()


async def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--postgres', action='store_true'); args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='website-auth-evidence-', dir=SERVER.parents[1] / 'tmp'))
    results = {'sqlite': await asyncio.to_thread(run_database, None, root / 'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_websiteauth_' + uuid.uuid4().hex
        assert re.fullmatch('tmp_vf_websiteauth_[0-9a-f]{32}', name)
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"'); created = True
            results['postgresql'] = await asyncio.to_thread(run_database, url.set(database=name).render_as_string(hide_password=False), root / 'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (root / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print(json.dumps({'passed': True, 'counts': {k: v['count'] for k, v in results.items()}, 'evidence': str(root)}))


if __name__ == '__main__': asyncio.run(main())
