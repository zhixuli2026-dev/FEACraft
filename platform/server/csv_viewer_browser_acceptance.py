"""Exercise a large real CSV and related text fixtures in the workbench."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx


CASE = 'CSV viewer real files'
LARGE = 'Ref/runs.csv'
TSV = 'Ref/controls.tsv'
SLOW = 'Ref/slow.csv'
MALFORMED = 'Ref/uneven.csv'
EMPTY = 'Ref/empty.csv'
ROWS = 60_000
HEADER = ['序号', '压力', '说明', '样本', '来源', '注释', '预留', '批次']
FIRST = ['1', '-2.5e-3', '带逗号, 和双引号 "保留"\n第二行', '第一行', '站点 A', '=SUM(1,2)', '', 'first']

APP = '''
import asyncio, json, os, time
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from app.main import app
from app.services.process_identity_native import observe

root = Path(__file__).parent
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

@app.middleware('http')
async def delay_selected_file(request, call_next):
    if request.url.path.endswith('/file') and request.query_params.get('path') == SLOW:
        editor_read = root/'slow-editor-read'
        if not editor_read.exists():
            editor_read.touch()
            return await call_next(request)
        (root/'slow-read-entered').touch()
        deadline = time.monotonic() + 25
        while not (root/'release-slow-read').exists() and time.monotonic() < deadline:
            await asyncio.sleep(.03)
    return await call_next(request)

app.mount('/', StaticFiles(directory=DIST, html=True))
'''


def large_csv():
    with io.StringIO(newline='') as output:
        writer = csv.writer(output)
        writer.writerow(HEADER)
        writer.writerow(FIRST)
        for index in range(2, ROWS + 1):
            number = -999 if index == 2 else 999999 if index == ROWS else index * 3
            marker = '末尾唯一样本' if index == ROWS else f'run-{index:05d}'
            writer.writerow([index, number, marker, f'S{index}', '站点 B', '', '', 'even' if index % 2 == 0 else 'odd'])
        return output.getvalue().encode('utf-8-sig')


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    receipt = {'passed': False, 'baseline': settings['baseline'], 'page_errors': [], 'inference_requests': []}
    with sync_playwright() as toolkit:
        edge = toolkit.chromium.launch(channel='msedge')
        receipt['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1480, 'height': 900}, accept_downloads=True, locale='zh-CN')
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(25_000)
        expect.set_options(timeout=15_000)
        page.on('pageerror', lambda error: receipt['page_errors'].append(str(error)))
        page.on('request', lambda request: receipt['inference_requests'].append(urlsplit(request.url).path)
                if request.method == 'POST' and urlsplit(request.url).path.endswith(('/messages', '/turns', '/goal:run')) else None)
        viewer = page.get_by_test_id('csv-viewer')
        viewport = page.get_by_test_id('csv-viewport')
        search = page.get_by_test_id('csv-search')
        count = page.get_by_test_id('csv-count')
        rows = page.get_by_test_id('csv-row')

        def open_file(path):
            page.locator('[data-path=' + json.dumps(path) + ']').click()
            expect(viewer).to_have_attribute('data-file', path)

        def source_row(index):
            return viewer.locator(f'[data-source-row="{index}"]')

        def download_button(label, name):
            with page.expect_download() as event:
                page.get_by_role('button', name=label, exact=True).click()
            download = event.value
            assert download.suggested_filename == 'runs-导出.csv', download.suggested_filename
            path = root / name
            download.save_as(path)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                return list(csv.reader(stream))

        try:
            page.goto(base)
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            folder = page.locator('[data-path="Ref"]')
            expect(folder).to_be_visible()
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            started = time.monotonic()
            open_file(LARGE)
            expect(viewer.get_by_test_id('csv-header')).to_have_count(len(HEADER))
            receipt['seconds_from_tree_click_to_headers'] = round(time.monotonic() - started, 3)
            if settings['baseline']:
                expect(viewer.locator('tbody tr')).to_have_count(2000)
                expect(viewer.get_by_text('末尾唯一样本', exact=True)).to_have_count(0)
                receipt['mounted_rows'] = 2000
                receipt['old_truncation'] = True
                page.screenshot(path=str(root/'csv-baseline.png'))
                receipt['passed'] = True
                return

            expect(count).to_have_text('60,000 / 60,000 行 · 8 列')
            expect(source_row(1)).to_be_visible()
            expect(source_row(ROWS)).to_have_count(0)
            mounted = rows.count()
            assert 0 < mounted < 50, mounted
            receipt['data_rows'] = ROWS
            receipt['initial_mounted_rows'] = mounted
            assert source_row(1).bounding_box()['height'] == 32
            source_row(1).get_by_role('button', name='查看第 1 行').click()
            expect(page.get_by_test_id('csv-detail').get_by_text('第二行')).to_be_visible()
            assert FIRST[2] in page.get_by_test_id('csv-detail').inner_text()
            page.screenshot(path=str(root/'csv-row-detail.png'))
            page.get_by_role('button', name='关闭行详情').click()
            source_row(1).get_by_role('checkbox').check()
            page.get_by_role('spinbutton', name='筛选结果中的行号').fill(str(ROWS))
            page.get_by_role('button', name='跳转到行').click()
            expect(source_row(ROWS)).to_contain_text('末尾唯一样本')
            bottom, frame = source_row(ROWS).bounding_box(), viewport.bounding_box()
            assert bottom['y'] >= frame['y'] and bottom['y'] + bottom['height'] <= frame['y'] + frame['height'], (bottom, frame)
            assert 0 < rows.count() < 50
            receipt['last_row_in_view'] = True
            page.screenshot(path=str(root/'csv-last-row.png'))

            pressure = viewer.get_by_test_id('csv-header').filter(has_text='压力')
            pressure.get_by_role('button').click()
            expect(pressure).to_have_attribute('aria-sort', 'ascending')
            expect(rows.first).to_have_attribute('data-source-row', '2')
            pressure.get_by_role('button').click()
            expect(rows.first).to_have_attribute('data-source-row', str(ROWS))
            pressure.get_by_role('button').click()
            expect(rows.first).to_have_attribute('data-source-row', '1')
            column = page.get_by_role('combobox', name='筛选列')
            column.select_option('4')
            search.fill('末尾唯一样本')
            expect(count).to_contain_text('0 / 60,000')
            expect(viewer.get_by_text('没有匹配的行。')).to_be_visible()
            column.select_option('2')
            expect(count).to_contain_text('1 / 60,000')
            expect(source_row(ROWS)).to_be_visible()
            page.get_by_role('checkbox', name='选择全部筛选结果').check()
            expect(page.get_by_test_id('csv-selection')).to_have_text('已选 2 行')
            selected = download_button('导出所选', 'selected.csv')
            assert selected[0] == HEADER and len(selected) == 3
            assert selected[1] == [*FIRST[:5], "'"+FIRST[5], *FIRST[6:]], selected[1]
            assert selected[2][0] == str(ROWS) and selected[2][2] == '末尾唯一样本'
            column.select_option('7')
            search.fill('even')
            expect(count).to_contain_text('30,000 / 60,000')
            exported = download_button('导出筛选结果', 'filtered.csv')
            assert len(exported) == 30_001 and exported[0] == HEADER and exported[1][0] == '2'
            assert exported[-1][0] == str(ROWS) and all(row[7] == 'even' for row in exported[1:])
            receipt['selected_and_filtered_rows_exported'] = [len(selected) - 1, len(exported) - 1]

            open_file(MALFORMED)
            warning = viewer.get_by_test_id('csv-warning')
            expect(warning).to_be_visible()
            warning.locator('summary').click()
            expect(warning).to_contain_text('行与表头列数不同')
            expect(source_row(2)).to_contain_text('额外值')
            open_file(EMPTY)
            expect(viewer.get_by_text('文件没有表格数据。')).to_be_visible()
            open_file(SLOW)
            expect(viewer).to_have_attribute('aria-busy', 'true')
            marker = root/'slow-read-entered'
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                page.wait_for_timeout(30)
            assert marker.exists()
            open_file(TSV)
            expect(viewer.get_by_text('含制表符', exact=False)).to_be_visible()
            def slow_file(request):
                return parse_qs(urlsplit(request.url).query).get('path') == [SLOW]

            with page.expect_event('requestfinished', predicate=slow_file):
                with page.expect_response(lambda response: slow_file(response.request)) as delayed:
                    (root/'release-slow-read').touch()
            assert delayed.value.status == 200
            page.evaluate('() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))')
            expect(viewer).to_have_attribute('data-file', TSV)
            expect(viewer.get_by_text('含制表符', exact=False)).to_be_visible()
            expect(viewer.get_by_text('过期 CSV', exact=True)).to_have_count(0)
            expect(page.get_by_test_id('csv-selection')).to_have_text('已选 0 行')
            receipt['late_csv_did_not_replace_tsv'] = True
            page.set_viewport_size({'width': 980, 'height': 760})
            expect(page.get_by_role('button', name='导出筛选结果')).to_be_visible()
            page.screenshot(path=str(root/'csv-small-workbench.png'))
            assert not receipt['page_errors'] and not receipt['inference_requests'], receipt
            receipt['passed'] = True
        finally:
            (root/'release-slow-read').touch()
            (root/'browser.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
            if not receipt['passed']:
                page.screenshot(path=str(root/'csv-failure.png'), timeout=4000)
            edge.close()


def run(args):
    from app.services.process_identity_native import probe
    from testsupport import IsolatedServer

    api = IsolatedServer('csv-viewer-browser', app='csv_fixture:app', access_log=True)
    root = api.root
    print('CSV_VIEWER_EVIDENCE', root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    (root/'csv_fixture.py').write_text(f'DIST = {str(dist)!r}\nSLOW = {SLOW!r}\n'+APP, encoding='utf-8')
    temp = root/'browser-tmp'
    temp.mkdir()
    receipt = {'passed': False, 'baseline': args.baseline, 'model_calls': 0, 'mock_llm_calls': 0}
    try:
        with api:
            api.create_case(CASE)
            prefix = api.prefix
            data = large_csv()
            upload = httpx.post(api.base+'/api/v1'+prefix+'/upload', params={'path': LARGE, 'expected_absent': 'true'},
                headers={'Authorization': 'Bearer '+api.token}, files={'file': ('runs.csv', data, 'text/csv')},
                timeout=30, trust_env=False)
            assert upload.status_code == 200, (upload.status_code, upload.text[:360])
            receipt['csv_bytes_uploaded'] = len(data)
            api.put(TSV, '标签\t值\r\n"含制表符\t的值"\t"二"\r\n')
            tsv = api.call('GET', prefix+'/file?'+urlencode({'path': TSV}))
            if not args.baseline:
                assert tsv['kind'] == 'text' and '含制表符\t的值' in tsv['content']
            api.put(MALFORMED, '名称,结果\n甲,\n乙,2,额外值\n"未闭合,3')
            api.put(EMPTY, '')
            api.put(SLOW, '名称,数据\n过期 CSV,111')
            report = {'root': str(root), 'base': api.base, 'token': api.token, 'baseline': args.baseline}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(report), text=True, encoding='utf8', check=True, timeout=220,
                env=dict(os.environ, TMP=str(temp), TEMP=str(temp)))
            receipt['browser'] = json.loads((root/'browser.json').read_text(encoding='utf-8'))
            identity = json.loads((root/'api-birth.json').read_text(encoding='utf-8'))
            api.create_case('Other CSV account')
            api.call('GET', prefix+'/file?'+urlencode({'path': LARGE}), expect=404)
            api.token = None
            api.call('GET', prefix+'/file?'+urlencode({'path': LARGE}), expect=401)
            receipt['outsider_and_anonymous'] = [404, 401]
        assert api.proc.poll() is not None
        api.proc = None
        deadline = time.monotonic() + 10
        while probe(identity)['state'] != 'exited' and time.monotonic() < deadline:
            time.sleep(.05)
        assert probe(identity)['state'] == 'exited'
        with socket.socket() as connection:
            connection.settimeout(.25)
            assert connection.connect_ex(('127.0.0.1', api.port)) != 0
        receipt['api_exited'] = receipt['passed'] = True
    finally:
        (root/'csv-viewer-result.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf8')
    print('CSV_VIEWER_PASS', root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', required=True, type=Path)
        parser.add_argument('--browser-python', required=True, type=Path)
        parser.add_argument('--baseline', action='store_true')
        run(parser.parse_args())
