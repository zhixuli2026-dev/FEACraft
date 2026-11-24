"""SCNET account protocol over loopback HTTP and the actual web panel in Edge."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
import hmac
import json
from pathlib import Path
from threading import Event
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import inspect, text

from scnet_request_acceptance import serve
from testsupport import IsolatedServer
from workflow_migration_smoke import alembic, engine_for


class Peer:
    def __init__(self):
        self.login = {'username':'scnet-verification', 'access_key':uuid4().hex, 'secret_key':uuid4().hex}
        self.tokens = {key:uuid4().hex for key in ('platform', 'north', 'south', 'renewed')}
        self.requests = []
        self.hold_renewal = False
        self.entered = Event()
        self.release = Event()

    def handler(self):
        peer = self

        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, data, status=200):
                raw = json.dumps({'code':'0' if status == 200 else '401', 'data':data}, ensure_ascii=False).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def realm(self):
                if any(field in self.headers for field in ('Cookie', 'accessKey', 'signature', 'secretKey', 'X-Signature')):
                    return None
                return next((key for key, token in peer.tokens.items() if token == self.headers.get('token')), None)

            def do_POST(self):
                if self.path == '/api/user/v3/tokens':
                    fields = {key:self.headers.get(key, '') for key in ('accessKey', 'timestamp', 'user')}
                    raw = json.dumps(fields, separators=(',', ':')).encode()
                    signature = hmac.digest(peer.login['secret_key'].encode(), raw, 'sha256').hex()
                    valid = (fields['accessKey'] == peer.login['access_key'] and fields['user'] == peer.login['username']
                             and fields['timestamp'].isdigit() and abs(int(fields['timestamp'])-time.time()) < 60
                             and hmac.compare_digest(signature, self.headers.get('signature', ''))
                             and 'token' not in self.headers and peer.login['secret_key'] not in str(self.headers))
                    if not valid:
                        return self.reply(None, 401)
                    peer.requests.append(('POST', self.path, 'signed'))
                    return self.reply([
                        {'clusterId':'0', 'token':peer.tokens['platform']},
                        {'clusterId':'101', 'clusterName':'北区', 'token':peer.tokens['north']},
                        {'clusterId':'202', 'clusterName':'南区', 'token':peer.tokens['south']},
                        {'clusterId':'303', 'clusterName':'未激活区域', 'token':None},
                    ])
                if self.path == '/ac/openapi/v2/tokens/next' and self.realm() == 'north':
                    peer.requests.append(('POST', self.path, 'north'))
                    if peer.hold_renewal:
                        peer.entered.set()
                        if not peer.release.wait(8):
                            return self.reply(None, 401)
                    return self.reply(peer.tokens['renewed'])
                self.reply(None, 401)

            def do_GET(self):
                target = urlsplit(self.path)
                path, realm = target.path, self.realm()
                peer.requests.append(('GET', path, realm))
                if path == '/ac/openapi/v2/user' and realm == 'platform':
                    return self.reply({'userName':peer.login['username'], 'accountName':'协议验收账户',
                        'accountStatus':'active', 'internalToken':peer.tokens['platform']})
                if path == '/ac/openapi/v2/center' and realm in {'north','south'}:
                    north = realm == 'north'
                    region = {'id':'101' if north else '202', 'hpcUrls':[
                        {'enable':False,'url':peer.origin + '/disabled'},
                        {'enable':True,'url':'http://[invalid'},
                        {'enable':True,'url':peer.origin + '/' + realm + '/hpc'},
                    ]}
                    if north:
                        region['aiUrls'] = [{'enable':True, 'url':peer.origin + '/north/ai'}]
                    return self.reply(region if north else [region])
                if realm not in {'north','south','renewed'}:
                    return self.reply(None, 401)
                source = 'north' if realm == 'renewed' else realm
                prefix = '/' + source + '/hpc/openapi/v2'
                job = {'jobId':710 if source == 'north' else 820, 'jobName':'北区仿真' if source == 'north' else '南区仿真',
                       'jobState':'statR', 'token':peer.tokens[realm]}
                if path == prefix + '/cluster':
                    return self.reply([{'id':17 if source == 'north' else 28}])
                if path == prefix + '/jobs':
                    manager = '17' if source == 'north' else '28'
                    if parse_qs(target.query) != {'strClusterIDList':[manager], 'start':['0'], 'limit':['25']}:
                        return self.reply(None, 401)
                    return self.reply({'list':[job], 'total':1})
                if path == prefix + '/jobs/' + str(job['jobId']):
                    return self.reply(job)
                if path == prefix + '/desktop/vnc':
                    return self.reply({'list':[{'sessionId':'desktop-4','name':'远程图形会话','password':peer.tokens[realm]}], 'total':1})
                self.reply(None, 401)

        return Endpoint


def reference(account, region='101'):
    return {'session_id':account['id'], 'revision':account['revision'], 'region_id':region}


async def database_state(database, *, session=False):
    engine = engine_for(database)
    try:
        async with engine.connect() as connection:
            if session:
                result = await connection.execute(text('SELECT state_cipher, revision, renewal_until FROM scnet_account_sessions'))
                return result.one()
            return await connection.run_sync(lambda conn: inspect(conn).has_table('scnet_account_sessions'))
    finally:
        await engine.dispose()


def verify(api, peer, vault, database):
    api.create_case('SCNET account owner')
    owner = api.token
    post = lambda path, body, **options: api.call('POST', '/scnet/' + path, body, **options)
    invalid = post('account:connect', {'secret_key':peer.login['secret_key']}, expect=422, raw=True)
    assert peer.login['secret_key'].encode() not in invalid
    post('account:connect', {**peer.login, 'secret_key':uuid4().hex}, expect=502)
    account = post('account:connect', peer.login)
    public = json.dumps(account)
    secrets = [*peer.tokens.values(), peer.login['access_key'], peer.login['secret_key']]
    assert all(secret not in public for secret in secrets), 'public account contains credentials'
    assert account['account']['accountName'] == '协议验收账户' and account['revision'] == 1
    assert [(row['id'], row['services'], row['can_renew']) for row in account['regions']] == [
        ('101', ['hpc','ai'], True), ('202', ['hpc'], True), ('303', [], False)]
    cipher, revision, lease = asyncio.run(database_state(database, session=True))
    assert (revision, lease) == (1, 0) and all(secret not in cipher for secret in secrets)
    state = Fernet(vault).decrypt(cipher.encode()).decode()
    assert peer.tokens['north'] in state and peer.tokens['south'] in state
    assert all(secret not in state for secret in (peer.tokens['platform'], *secrets[-2:]))
    assert post('jobs.list', reference(account))[0]['token'] == '***'
    assert post('jobs.list', reference(account, '202'))[0]['jobName'] == '南区仿真'
    assert post('desktops.list', reference(account))[0]['password'] == '***'
    detail = post('jobs.detail', {**reference(account), 'args':{'id':710}, 'detail_view':True})
    assert '北区仿真' in detail['copy_text']
    post('jobs.list', reference(account, '303'), expect=409)
    post('jobs.submit', {**reference(account), 'confirmed':True}, expect=409)
    api.create_case('Other SCNET account owner')
    post('jobs.list', reference(account), expect=404)
    post('account:renew', reference(account), expect=404)
    post('account:logout', reference(account))
    api.token = owner
    renewed = post('account:renew', reference(account))
    assert renewed['revision'] == 2
    post('jobs.list', reference(account), expect=409)
    assert post('jobs.list', reference(renewed))[0]['jobId'] == 710
    assert ('GET', '/north/hpc/openapi/v2/jobs', 'renewed') in peer.requests
    post('account:logout', reference(renewed))
    post('jobs.list', reference(renewed), expect=404)

    account = post('account:connect', peer.login)
    peer.hold_renewal = True
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(post, 'account:renew', reference(account), expect=409)
        try:
            assert peer.entered.wait(5), 'renewal did not reach the upstream'
            count = len(peer.requests)
            post('account:renew', reference(account), expect=409)
            assert len(peer.requests) == count, 'concurrent renewal reached upstream'
            post('account:logout', reference(account))
        finally:
            peer.release.set()
        pending.result(timeout=10)
    peer.hold_renewal = False
    post('jobs.list', reference(account), expect=404)
    assert all(secret not in (api.root / 'api.log').read_text(encoding='utf-8') for secret in secrets)


def browser(api, peer):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        try:
            page = edge.new_page(viewport={'width':730,'height':920}, locale='zh-CN')
            page.add_init_script(f'if(location.origin==={json.dumps(api.base)})'
                                 f'localStorage.setItem("vf_token",{json.dumps(api.token)});')
            page.goto(api.base, wait_until='domcontentloaded')
            page.get_by_label('工作面板', exact=True).select_option('scnet')
            panel = page.locator('.scnet-workbench')
            status = panel.get_by_test_id('scnet-status')
            panel.get_by_label('SCNET 用户名', exact=True).fill(peer.login['username'])
            panel.get_by_label('Access Key', exact=True).fill(peer.login['access_key'])
            panel.get_by_test_id('scnet-secret').fill(peer.login['secret_key'])
            with page.expect_response(lambda response: response.url.endswith('/account:connect')) as pending:
                panel.get_by_role('button', name='连接 SCNET 账户').click()
            account = pending.value.json()
            assert account['revision'] == 1
            expect(status).to_have_text('SCNET 账户已连接')
            expect(panel.get_by_text('协议验收账户', exact=True)).to_be_visible()
            expect(panel.get_by_label('区域', exact=True)).to_have_value('101')
            panel.screenshot(path=api.root/'account-730.png')
            page.set_viewport_size({'width':390,'height':840})
            assert panel.locator('.scnet-account-summary').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
            panel.screenshot(path=api.root/'account-390.png')
            panel.get_by_label('区域', exact=True).select_option('202')
            panel.get_by_test_id('scnet-tab-jobs').click()
            expect(panel.get_by_text('南区仿真', exact=True)).to_be_visible()
            panel.get_by_role('button', name='详情', exact=True).click()
            expect(panel.get_by_test_id('scnet-detail')).to_be_visible()
            panel.get_by_test_id('scnet-tab-connect').click()
            panel.get_by_label('区域', exact=True).select_option('101')
            panel.get_by_role('button', name='续约区域凭证').click()
            expect(status).to_have_text('当前区域凭证已续约')
            panel.get_by_test_id('scnet-tab-desktops').click()
            expect(panel.get_by_text('远程图形会话', exact=True)).to_be_visible()
            assert ('GET', '/north/hpc/openapi/v2/desktop/vnc', 'renewed') in peer.requests
            panel.get_by_test_id('scnet-tab-connect').click()
            panel.get_by_role('button', name='注销', exact=True).click()
            expect(status).to_have_text('SCNET 会话已注销')
            expect(panel.get_by_test_id('scnet-secret')).to_have_value('')
            api.call('POST','/scnet/jobs.list',{**reference(account),'revision':2},expect=404)
        finally:
            edge.close()


def main(dist, database_url=None):
    peer = Peer()
    vault = Fernet.generate_key()
    api = IsolatedServer('scnet-account-acceptance', app='scnet_fixture:app', database_url=database_url)
    database = database_url or 'sqlite+aiosqlite:///' + (api.root / 'test.sqlite').as_posix()
    alembic(database, api.root, 'upgrade', 'head')
    alembic(database, api.root, 'downgrade', 'f71a46d08e9b')
    assert not asyncio.run(database_state(database))
    alembic(database, api.root, 'upgrade', 'head')
    vault_path = api.root / 'vault.key'
    vault_path.write_bytes(vault)
    with serve(peer.handler()) as origin:
        peer.origin = origin
        settings = dict(VIBEFLOW_SCNET_AUTH_ORIGIN=origin, VIBEFLOW_SCNET_PLATFORM_ORIGIN=origin,
                        VIBEFLOW_VAULT_KEY_PATH=str(vault_path))
        fixture = 'import os\nos.environ.update(' + repr(settings) + ')\nfrom app.main import app\n'
        if dist:
            fixture += 'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
        (api.root/'scnet_fixture.py').write_text(fixture, encoding='utf-8')
        try:
            with api:
                verify(api, peer, vault, database)
                if dist:
                    browser(api, peer)
        finally:
            vault_path.unlink(missing_ok=True)
    result = {'status':'passed', 'upstream':'signed loopback HTTP', 'live_cloud':False, 'real_llm_calls':0,
              'database':'postgresql' if database_url else 'sqlite',
              'browser':'Microsoft Edge' if dist else None, 'upstream_requests':len(peer.requests),
              'upgrade_downgrade_upgrade':True, 'lease_race_verified':True}
    path = api.root/'account-result.json'
    path.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('SCNET_ACCOUNT_EVIDENCE=' + str(path), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path)
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    dist = args.dist.resolve() if args.dist else None
    if args.postgres:
        from mixed_read_capacity_checks import postgres
        asyncio.run(postgres(None, None, verifier=lambda _, url, _group: main(dist, url)))
    else:
        main(dist)
