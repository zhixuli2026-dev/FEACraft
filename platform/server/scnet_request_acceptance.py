"""Verify SCNET confirmation, redirects and disconnects with signed local HTTP and Edge."""

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import socket
from threading import Event, Thread
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app.services.scnet import compact_sign
from testsupport import IsolatedServer


@contextmanager
def serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


class Peer:
    def __init__(self):
        self.writes = []
        self.reads = []
        self.other_origin_requests = 0
        self.created = [{'id':'job-existing', 'name':'已存在的作业', 'status':'running'}]
        self.hold = None
        self.entered = Event()
        self.disconnected = Event()
        self.timeout_chunks = 0
        self.credentials = {name:f'fixture-{uuid4().hex}' for name in ('access_key','secret_key','token')}

    def arm(self, operation):
        self.entered.clear()
        self.disconnected.clear()
        self.hold = operation

    def hold_response(self, connection):
        self.entered.set()
        readable, _, _ = select.select([connection], [], [], 8)
        if readable and connection.recv(1, socket.MSG_PEEK) == b'':
            self.disconnected.set()

    @contextmanager
    def endpoint(self):
        peer = self
        signature = compact_sign({'accessKey':self.credentials['access_key'],'token':self.credentials['token']},
                                 self.credentials['secret_key'])

        class Endpoint(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.settimeout(15)

            def log_message(self, *_args):
                pass

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionError, TimeoutError):
                    pass

            def respond(self, payload, status=200):
                data = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def redirect(self, address):
                self.send_response(307)
                self.send_header('Location', address)
                self.send_header('Set-Cookie', 'scnet-fixture=unused; Path=/')
                self.send_header('Content-Length','0')
                self.end_headers()

            def authenticated(self):
                if (self.headers.get('X-Signature') == signature
                        and self.headers.get('X-Access-Key') == peer.credentials['access_key']
                        and self.headers.get('token') == peer.credentials['token'] and 'Cookie' not in self.headers):
                    return True
                self.respond({'msg':'凭据无效'}, 401)
                return False

            def do_GET(self):
                if not self.authenticated():
                    return
                url = urlsplit(self.path)
                peer.reads.append(url.path)
                query = parse_qs(url.query)
                if url.path.endswith('/jobs'):
                    mode = query.get('mode', [''])[0]
                    if mode == 'redirect':
                        return self.redirect('listed')
                    if mode == 'foreign':
                        return self.redirect(peer.other_url + '/unused')
                    if mode == 'cycle':
                        return self.redirect(self.path)
                    if peer.hold == 'list':
                        peer.hold = None
                        peer.hold_response(self.connection)
                    return self.respond({'records':peer.created})
                if url.path.endswith('/listed'):
                    return self.respond({'records':peer.created})
                if url.path.endswith('/jobs/job-existing'):
                    if peer.hold == 'detail':
                        peer.hold = None
                        peer.hold_response(self.connection)
                    return self.respond({'data':peer.created[0],'code':'0'})
                if url.path.endswith('/files'):
                    return self.respond({'fileList':[{'name':'本地协议文件','path':'verified.txt'}]})
                if url.path.endswith('/files:download'):
                    return self.redirect('download-content')
                if url.path.endswith('/download-content'):
                    return self.respond({'content':'已读回同源文件'})
                return self.respond({'msg':'未找到资源'}, 404)

            def do_POST(self):
                if not self.authenticated():
                    return
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                name = data['name']
                peer.writes.append((urlsplit(self.path).path, name))
                if name == '重定向试验' and self.path.endswith('/jobs'):
                    return self.redirect('mutation-target')
                if name == '持续返回仍超时':
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length','100')
                    self.end_headers()
                    for _ in range(14):
                        self.wfile.write(b' ')
                        self.wfile.flush()
                        peer.timeout_chunks += 1
                        time.sleep(1)
                    return
                row = {'id':f'job-{len(peer.writes)}','name':name,'status':'running'}
                peer.created.append(row)
                if peer.hold == 'write':
                    peer.hold = None
                    peer.hold_response(self.connection)
                return self.respond(row)

        class OtherOrigin(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                peer.other_origin_requests += 1
                self.send_error(403)

        with serve(OtherOrigin) as other_url, serve(Endpoint) as base_url:
            self.other_url = other_url
            yield {'base_url':base_url, **self.credentials}


def check_http(api, settings, peer):
    def post(operation, args, confirmed=False, expect=200):
        return api.call('POST', '/scnet/' + operation, {**settings, 'args':args, 'confirmed':confirmed}, expect=expect)

    unconfirmed = {'job':{'name':'未确认'}}
    post('jobs.submit', unconfirmed, expect=400)
    assert not peer.writes
    assert post('jobs.list',{'query':{'mode':'redirect'}}) == peer.created
    assert len(peer.reads) == 2 and peer.reads[-1].endswith('/listed')
    refused = post('jobs.list', {'query':{'mode':'foreign'}}, expect=502)
    assert refused['detail']['code'] == 'scnet_redirect_refused' and not peer.other_origin_requests
    old_reads = len(peer.reads)
    refused = post('jobs.list', {'query':{'mode':'cycle'}}, expect=502)
    assert refused['detail']['code'] == 'scnet_redirect_refused' and len(peer.reads) - old_reads == 4
    assert post('files.download', {'path':'verified.txt'}) == '已读回同源文件'
    downloaded = api.call('POST', '/scnet/files:download', {**settings, 'args':{'path':'verified.txt'}}, raw=True)
    assert downloaded.decode() == '已读回同源文件'
    refused = post('jobs.submit', {'job':{'name':'重定向试验'}}, confirmed=True, expect=502)
    assert refused['detail']['code'] == 'scnet_mutation_redirect' and len(peer.writes) == 1
    started = time.monotonic()
    timed_out = post('jobs.submit', {'job':{'name':'持续返回仍超时'}}, confirmed=True, expect=502)
    elapsed = time.monotonic() - started
    assert timed_out['detail']['code'] == 'scnet_timeout' and 9.5 <= elapsed < 14
    assert len(peer.writes) == 2 and peer.timeout_chunks >= 8
    return {'unconfirmed_writes':0, 'other_origin_requests':0, 'redirect_hops_at_limit':3, 'redirect_cookie_requests':0,
            'download_paths_checked':2, 'mutation_attempts_for_redirect':1,
            'mutation_attempts_for_timeout':1, 'timeout_seconds':round(elapsed,3)}


def browser(api, settings, peer):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        page = edge.new_page(viewport={'width':730,'height':920},locale='zh-CN')
        page.add_init_script(f'if(location.origin==={json.dumps(api.base)})'
                             f'localStorage.setItem("vf_token",{json.dumps(api.token)});')
        page.goto(api.base,wait_until='domcontentloaded')
        page.get_by_label('工作面板',exact=True).select_option('scnet')
        status = page.get_by_test_id('scnet-status')
        connect = page.get_by_test_id('scnet-connect')
        access = page.get_by_label('Access Key', exact=True)
        secret = page.get_by_test_id('scnet-secret')
        token = page.get_by_label('会话令牌', exact=True)
        expect(connect).to_be_disabled()
        page.get_by_test_id('scnet-base-url').fill(settings['base_url'])
        access.fill(settings['access_key'])
        secret.fill(settings['secret_key'])
        token.fill('invalid-fixture-token')
        connect.click()
        expect(status).to_contain_text('凭据无效')
        expect(connect).to_be_visible()
        token.fill(settings['token'])
        connect.click()
        expect(status).to_contain_text('jobs.list 完成')
        before = len(peer.writes)
        submit = page.get_by_test_id('scnet-job-submit')
        submit.locator('..').locator('input').first.fill('停止等待仍保留')
        page.once('dialog', lambda dialog: dialog.dismiss())
        submit.click()
        expect(status).to_contain_text('请求发送次数为 0')
        assert len(peer.writes) == before

        submit.locator('..').locator('input').first.fill('确认后成功提交')
        page.once('dialog', lambda dialog: dialog.accept())
        submit.click()
        expect(status).to_contain_text('jobs.submit 完成')
        table = page.get_by_test_id('scnet-table')
        expect(table).to_contain_text('确认后成功提交')
        assert len(peer.writes) == before+1

        submit.locator('..').locator('input').first.fill('停止等待仍保留')
        peer.arm('write')
        messages = []
        def accept(dialog):
            messages.append(dialog.message)
            dialog.accept()
        page.once('dialog', accept)
        submit.click()
        assert peer.entered.wait(6) and len(peer.writes) == before+2
        assert '可能产生费用' in messages[0] and '停止等待仍保留' in messages[0]
        expect(submit).to_be_disabled()
        page.get_by_role('button',name='停止等待',exact=True).click()
        expect(status).to_contain_text('远端结果待确认')
        expect(submit).to_be_disabled()
        assert submit.evaluate('(button) => Number(getComputedStyle(button).opacity)') < 1
        assert peer.disconnected.wait(6), '应用取消后仍占用上游连接'
        page.screenshot(path=str(api.root/'scnet-stop-waiting-730.png'))
        page.set_viewport_size({'width':390,'height':860})
        expect(status).to_contain_text('远端结果待确认')
        page.screenshot(path=str(api.root/'scnet-stop-waiting-390.png'))
        page.set_viewport_size({'width':730,'height':920})
        page.get_by_role('button',name='刷新',exact=True).click()
        expect(table).to_contain_text('停止等待仍保留')
        expect(submit).to_be_enabled()
        assert len(peer.writes) == before+2

        peer.arm('detail')
        table.get_by_role('button',name='详情',exact=True).first.click()
        assert peer.entered.wait(6)
        page.get_by_test_id('scnet-tab-files').click()
        expect(table).to_contain_text('本地协议文件')
        assert peer.disconnected.wait(6)
        expect(page.get_by_test_id('scnet-detail')).to_have_count(0)
        expect(status).to_contain_text('files.list 完成')

        page.get_by_test_id('scnet-tab-connect').click()
        peer.arm('list')
        connect.click()
        assert peer.entered.wait(6)
        page.get_by_label('区域',exact=True).select_option('region-b')
        assert peer.disconnected.wait(6)
        expect(status).to_contain_text('已切换区域')
        expect(token).to_have_value('')
        token.fill(settings['token'])
        page.get_by_role('button',name='注销',exact=True).click()
        for field in (access, secret, token):
            expect(field).to_have_value('')
        expect(connect).to_be_disabled()
        assert len(peer.writes) == before+2
        return {'edge':edge.version,'canceled_before_send':0,'normal_submit_sent':1,'aborted_submit_sent':1,
                'remote_result_remained_after_abort':True,'verified_upstream_disconnects':3,
                'invalid_credentials_stayed_disconnected':True,'logout_cleared_credentials':True,
                'write_waited_for_refresh':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path)
    options = parser.parse_args()
    isolate = IsolatedServer('scnet-request-acceptance',app='scnet_request_fixture:app')
    fixture = 'from app.main import app\n'
    if options.dist:
        dist = options.dist.resolve(strict=True)
        fixture += ('from fastapi.staticfiles import StaticFiles\n'
                    f'app.mount("/",StaticFiles(directory={str(dist)!r},html=True))\n')
    (isolate.root/'scnet_request_fixture.py').write_text(fixture,encoding='utf8')
    report = {'passed':False,'model_calls':0}
    print('SCNET_REQUEST_EVIDENCE', isolate.root, flush=True)
    peer = Peer()
    try:
        with peer.endpoint() as settings, isolate as api:
            api.create_case('SCNET request lifecycle')
            report['signed_http'] = check_http(api,settings,peer)
            if options.dist:
                report['browser'] = browser(api,settings,peer)
        report.update(passed=True,isolated_api_closed=api.proc.poll() is not None)
    finally:
        (isolate.root/'request-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')


if __name__ == '__main__':
    main()
