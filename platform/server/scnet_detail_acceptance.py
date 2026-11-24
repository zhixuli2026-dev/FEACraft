"""Read-only SCNET detail acceptance with signed loopback HTTP and the built Edge UI."""

import argparse
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app.services.scnet import compact_sign
from testsupport import IsolatedServer


@contextmanager
def provider():
    credentials = {key: f'fixture-{uuid4().hex}' for key in ('access_key', 'secret_key', 'token')}
    sent = []
    job = {'jobId':'job-101', 'jobName':'风洞计算', 'jobStatus':'statR', 'jobRunTime':'00:04:20',
           'procNumReq':16, 'customMetric':'solver-residual-v2', 'schedulerExtension':{'tolerance':0.002},
           'jobVncSessionInfo':{'strSessionWidth':1280, 'loginPasswd':credentials['secret_key']},
           'serviceUrl':f'https://example.test/view?token={credentials["token"]}'}
    history = {'jobId':'job-88', 'jobmanagerId':'manager-7', 'jobName':'早期计算', 'jobState':'statC',
               'acctTime':'2026-11-24 10:25:00', 'jobWalltimeUsed':3725}
    container = {'id':'container-1', 'instanceServiceName':'网格容器', 'status':'Running', 'cpuNumber':8,
                 'containerPortInfoList':[{'containerPort':443, 'accessUrl':'https://example.test/container?ticket=ignored'}]}
    notebook = {'id':'notebook-1', 'notebookName':'仿真笔记', 'notebookStatus':'Terminated', 'ramSize':'32 GB',
                'sshPassword':credentials['secret_key'], 'message':credentials['access_key']}
    desktop = {'id':'db-1', 'sessionId':'desk-1', 'name':'工程桌面', 'width':'1920', 'length':'1080',
               'hostname':'node-a', 'novncUrl':f'/desktop?mm={credentials["token"]}'}
    rows = {
        '/hpc/openapi/v2/jobs': [job], '/hpc/openapi/v2/job-history': [history],
        '/ai/openapi/v2/containers': [container], '/ai/openapi/v2/notebooks': [notebook],
        '/hpc/openapi/v2/desktops': [desktop],
    }
    details = {
        '/hpc/openapi/v2/jobs/job-101': job,
        '/hpc/openapi/v2/jobs/job-many': {**{f'extra{i}':i for i in range(110)},
                                        'jobId':'job-many', 'jobName':'完整摘要', 'procNumReq':4},
        '/hpc/openapi/v2/historyjobs/manager-7/job-88': history,
        '/ai/openapi/v2/instance-service/container-1/detail': container,
        '/ai/openapi/v2/notebook/detail': notebook,
        '/hpc/openapi/v2/desktop/vnc': {'total':1, 'list':[desktop]},
    }
    signed = compact_sign({'accessKey':credentials['access_key'], 'token':credentials['token']},
                          credentials['secret_key'])

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, status, data):
            payload = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            url = urlsplit(self.path)
            if (self.headers.get('X-Signature') != signed or self.headers.get('X-Access-Key') != credentials['access_key']
                    or self.headers.get('token') != credentials['token']):
                return self.reply(401, {'msg':'需要有效签名'})
            query = parse_qs(url.query)
            sent.append(url.path)
            if url.path in rows:
                # List responses only contain the fields needed to select a resource.
                keep = {'id','jobId','jobName','jobStatus','jobState','jobmanagerId','acctTime','instanceServiceName',
                        'notebookName','notebookStatus','name','sessionId'}
                return self.reply(200, {'data': {'records': [{k:v for k,v in row.items() if k in keep}
                                                            for row in rows[url.path]]}})
            if url.path == '/ai/openapi/v2/notebook/detail' and query != {'notebookId':['notebook-1']}:
                return self.reply(400, {'msg':'实例编号缺失'})
            if url.path.endswith('/manager-7/job-88') and query != {'acctTime':[history['acctTime']]}:
                return self.reply(400, {'msg':'历史作业查询时间不匹配'})
            if url.path not in details:
                return self.reply(404, {'msg':'资源不存在'})
            self.reply(200, {'code':'0', 'msg':'success', 'data':details[url.path]})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', credentials, sent
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def check_http(api, settings, sent):
    scenarios = (
        ('jobs.detail', {'id':'job-101'}, '风洞计算', 'CPU'),
        ('jobs.history-detail', {'id':'job-88', 'manager_id':'manager-7', 'account_time':'2026-11-24 10:25:00'},
         '早期计算', '1h 2m 5s'),
        ('containers.detail', {'id':'container-1'}, '网格容器', '服务端口'),
        ('notebooks.detail', {'id':'notebook-1'}, '仿真笔记', '已关机'),
        ('desktops.detail', {'id':'desk-1'}, '工程桌面', '1920×1080'),
    )
    for operation, args, title, field in scenarios:
        result = api.call('POST', '/scnet/' + operation, {**settings, 'args':args, 'detail_view':True})
        assert title in result['summary'] and field in result['copy_text']
        wire = json.dumps(result, ensure_ascii=False)
        assert 'ticket=ignored' not in wire and all(settings[key] not in wire for key in ('access_key','secret_key','token'))
    legacy = api.call('POST', '/scnet/jobs.detail', {**settings, 'args':{'id':'job-101'}})
    assert legacy['data']['jobStatus'] == 'statR'
    assert legacy['data']['jobVncSessionInfo']['loginPasswd'] == '***'
    assert legacy['data']['serviceUrl'] == 'https://example.test/view'
    many = api.call('POST', '/scnet/jobs.detail', {**settings, 'detail_view':True, 'args':{'id':'job-many'}})
    assert many['summary'] == '完整摘要' and many['truncated'] and len(many['fields']) == 100
    assert any(field['label'] == '申请 CPU 核数' for field in many['fields'])
    api.call('POST', '/scnet/desktops.detail', {**settings, 'detail_view':True, 'args':{'id':'missing'}}, expect=502)
    requests = len(sent)
    api.call('POST', '/scnet/jobs.detail', {**settings, 'detail_view':True, 'args':{'id':None}}, expect=400)
    assert len(sent) == requests
    return [scenario[0] for scenario in scenarios]


def browser(api, settings):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width':730,'height':970}, locale='zh-CN')
        context.grant_permissions(['clipboard-read','clipboard-write'], origin=api.base)
        context.add_init_script(f'if(location.origin==={json.dumps(api.base)})'
                                f'localStorage.setItem("vf_token",{json.dumps(api.token)});')
        page = context.new_page()
        page.goto(api.base, wait_until='domcontentloaded')
        page.get_by_label('工作面板', exact=True).select_option('scnet')
        page.get_by_test_id('scnet-base-url').fill(settings['base_url'])
        page.get_by_label('Access Key', exact=True).fill(settings['access_key'])
        page.get_by_test_id('scnet-secret').fill(settings['secret_key'])
        page.get_by_label('会话令牌', exact=True).fill(settings['token'])
        page.get_by_test_id('scnet-connect').click()
        status = page.get_by_test_id('scnet-status')
        expect(status).to_contain_text('jobs.list 完成')
        table = page.get_by_test_id('scnet-table')
        detail = page.get_by_test_id('scnet-detail')
        table.get_by_role('button', name='详情', exact=True).click()
        expect(detail.get_by_role('heading', level=3)).to_contain_text('风洞计算 · 运行中')
        expect(detail.get_by_text('申请 CPU 核数', exact=True)).to_be_visible()
        detail.get_by_role('button', name='复制 作业名称', exact=True).click()
        expect(detail.get_by_role('status')).to_have_text('已复制')
        assert page.evaluate('navigator.clipboard.readText()') == '风洞计算'
        search = detail.get_by_role('searchbox', name='搜索详情字段')
        advanced = detail.get_by_role('button', name='其他字段 展开', exact=True)
        expect(advanced).to_have_attribute('aria-expanded','false')
        search.fill('residual-v2')
        expect(detail.get_by_text('solver-residual-v2', exact=True)).to_be_visible()
        search.fill('不存在的字段')
        expect(detail.get_by_text('没有匹配的字段')).to_be_visible()
        search.clear()
        expect(advanced).to_have_attribute('aria-expanded','false')
        advanced.focus()
        page.keyboard.press('Enter')
        expect(detail.get_by_text('solver-residual-v2', exact=True)).to_be_visible()
        detail.get_by_role('button', name='复制摘要').click()
        summary = page.evaluate('navigator.clipboard.readText()')
        assert 'solver-residual-v2' in summary and 'https://example.test/view' in summary
        assert all(settings[key] not in summary for key in ('access_key','secret_key','token'))
        detail.screenshot(path=str(api.root/'detail-jobs-730.png'))

        page.get_by_role('button', name='历史', exact=True).click()
        expect(status).to_contain_text('jobs.history 完成')
        table.get_by_role('button', name='详情', exact=True).click()
        expect(detail.get_by_text('1h 2m 5s', exact=True)).to_be_visible()
        expect(search).to_have_value('')
        for resource, text in (('containers','网格容器'),('notebooks','仿真笔记'),('desktops','工程桌面')):
            page.get_by_test_id(f'scnet-tab-{resource}').click()
            expect(status).to_contain_text(f'{resource}.list 完成')
            table.get_by_role('button', name='详情', exact=True).click()
            expect(detail.get_by_role('heading', level=3)).to_contain_text(text)
        expect(detail.get_by_text('1920×1080', exact=True)).to_be_visible()
        page.set_viewport_size({'width':390,'height':860})
        assert detail.evaluate('(panel) => panel.scrollWidth <= panel.clientWidth + 1')
        detail.screenshot(path=str(api.root/'detail-desktop-390.png'))
        detail.get_by_role('button', name='关闭详情').click()
        expect(detail).to_have_count(0)
        return {'edge':edge.version, 'viewports':[730,390], 'resource_details':5,
                'clipboard_field':True, 'clipboard_summary':True, 'search_and_fold':True}


def run(dist, url=None):
    api = IsolatedServer('scnet-detail-acceptance', app='scnet_detail_fixture:app', database_url=url)
    report = {'passed':False, 'database':'postgresql' if url else 'sqlite', 'model_calls':0}
    fixture = 'from app.main import app\n'
    if dist:
        assert (dist / 'index.html').is_file()
        fixture += ('from fastapi.staticfiles import StaticFiles\n'
                    f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n')
    (api.root / 'scnet_detail_fixture.py').write_text(fixture, encoding='utf8')
    print('SCNET_DETAIL_EVIDENCE', api.root, flush=True)
    try:
        with provider() as (base, credentials, sent), api:
            api.create_case('SCNET detail acceptance')
            settings = {'base_url':base, **credentials}
            report['http_details'] = check_http(api, settings, sent)
            if dist:
                report['browser'] = browser(api, settings)
        report.update(passed=True, isolated_api_closed=api.proc.poll() is not None, upstream_gets=sent)
    finally:
        (api.root / 'detail-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path)
    parser.add_argument('--postgres', action='store_true')
    options = parser.parse_args()
    dist = options.dist.resolve() if options.dist else None
    if options.postgres:
        from mixed_read_capacity_checks import postgres
        asyncio.run(postgres(None, 'scnetdetail', lambda _store, url, _group: run(dist, url)))
    else:
        run(dist)
