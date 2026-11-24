"""Measure real on-demand PDF transfer, streamed search and rotation in Edge."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

CASE = 'PDF reader large document'
LARGE = 'Ref/engineering-handbook.pdf'
OTHER = 'Ref/replacement.pdf'
PAGES = 56
APP = '''
import asyncio, time
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from app.main import app

root = Path(__file__).parent

@app.middleware('http')
async def hold_real_pdf_range(request, call_next):
    arm = root / 'arm-range'
    if (request.url.path.endswith('/raw') and request.query_params.get('path') == LARGE
            and request.headers.get('range') and arm.exists()):
        arm.unlink()
        (root / 'range-entered').touch()
        deadline = time.monotonic() + 20
        while not (root / 'release-range').exists() and time.monotonic() < deadline:
            await asyncio.sleep(.02)
    return await call_next(request)

app.mount('/', StaticFiles(directory=DIST, html=True))
'''


def make_pdf(path, pages):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject, RectangleObject

    writer, seeded = PdfWriter(), random.Random(7319)
    name = NameObject
    font = writer._add_object(DictionaryObject({
        name('/Type'): name('/Font'), name('/Subtype'): name('/Type1'), name('/BaseFont'): name('/Helvetica'),
    }))
    heavy = pages > 1
    for index in range(pages):
        page = writer.add_blank_page(width=700, height=520)
        fonts = DictionaryObject({name('/F1'): font, name('/F2'): writer._add_object(DictionaryObject({
            name('/Type'): name('/Font'), name('/Subtype'): name('/Type1'), name('/BaseFont'): name('/Helvetica-Bold'),
        }))})
        resources = DictionaryObject({name('/Font'): fonts})
        text = f'BT /F1 22 Tf 55 425 Td ({"SeriesFlow" if heavy else "ReplacementOnly"} page {index + 1}) Tj ET\n'
        if heavy:
            image = DecodedStreamObject()
            image.update({name(key): value for key, value in {
                '/Type': name('/XObject'), '/Subtype': name('/Image'), '/Width': NumberObject(640),
                '/Height': NumberObject(600), '/BitsPerComponent': NumberObject(8),
                '/ColorSpace': name('/DeviceRGB'),
            }.items()})
            image.set_data(seeded.randbytes(640 * 600 * 3))
            resources[name('/XObject')] = DictionaryObject({name('/Im1'): writer._add_object(image)})
            text += 'q 120 0 0 95 60 90 cm /Im1 Do Q\n'
        if heavy and index == pages - 1:
            page.rotate(90)
            page.cropbox = RectangleObject((20, 28, 681, 502))
            text += ('BT /F1 24 Tf 55 350 Td (Split) Tj /F2 24 Tf ( token) Tj ET\n'
                     'BT /F1 24 Tf 55 315 Td (Split) Tj /F2 24 Tf ( token) Tj ET\n')
        page[name('/Resources')] = resources
        content = DecodedStreamObject()
        content.set_data(text.encode('ascii'))
        page[name('/Contents')] = writer._add_object(content)
    if heavy:
        writer.add_outline_item('Rotated appendix', pages - 1)
    with path.open('wb') as output:
        writer.write(output)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    report = {'passed': False, 'baseline': settings['baseline'], 'page_errors': [], 'inference_requests': []}
    requests, failed = [], []
    with sync_playwright() as toolkit:
        edge = toolkit.chromium.launch(channel='msedge')
        report['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1550, 'height': 1020}, locale='zh-CN')
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(25_000)
        expect.set_options(timeout=25_000)
        page.on('pageerror', lambda error: report['page_errors'].append(str(error)))

        def selected_raw(url):
            parts = urlsplit(url)
            return parts.path.endswith('/raw') and parse_qs(parts.query).get('path') == [LARGE]

        def response_received(response):
            if selected_raw(response.url):
                headers = response.headers
                requests.append({'method': response.request.method, 'status': response.status,
                                 'bytes': int(headers.get('content-length', '0')),
                                 'range': headers.get('content-range'), 'version': headers.get('x-vibeflow-file-version')})

        def request_started(request):
            if request.method == 'POST' and urlsplit(request.url).path.endswith(('/messages', '/turns', '/goal:run')):
                report['inference_requests'].append(urlsplit(request.url).path)

        page.on('response', response_received)
        page.on('request', request_started)
        page.on('requestfailed', lambda req: failed.append(req.failure) if selected_raw(req.url) else None)
        viewer = page.get_by_test_id('pdf-viewer')
        results = page.get_by_test_id('pdf-search-result')
        highlights = page.get_by_test_id('pdf-search-highlight')
        search = page.get_by_test_id('pdf-search')
        search_run = page.get_by_test_id('pdf-search-run')
        notice = page.get_by_test_id('pdf-notice')

        def open_file(name):
            folder = page.locator('[data-path="Ref"]')
            expect(folder).to_be_visible()
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            page.locator('[data-path=' + json.dumps(name) + ']').click()
            expect(viewer).to_have_attribute('data-file', name)

        def visible_page(index, marker):
            expect(viewer).to_have_attribute('data-rendered-page', str(index))
            expect(viewer.locator('.textLayer')).to_contain_text(marker)

        def hold_next_range():
            (root / 'range-entered').unlink(missing_ok=True)
            (root / 'release-range').unlink(missing_ok=True)
            (root / 'arm-range').touch()

        def await_range():
            deadline = time.monotonic() + 15
            while not (root / 'range-entered').exists() and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert (root / 'range-entered').exists(), 'Search did not request the next real PDF byte interval'

        def assert_overlay():
            expect(highlights.first).to_be_visible()
            return viewer.evaluate(r"""viewer => {
                const layer = viewer.querySelector('.textLayer');
                const hits = [...viewer.querySelectorAll('[data-testid=pdf-search-highlight]')];
                const spans = [...layer.querySelectorAll('span')];
                const matched = spans.filter(span => /split|token/i.test(span.textContent));
                const canvas = viewer.querySelector('canvas');
                const origin = canvas.getBoundingClientRect();
                const pixels = canvas.getContext('2d');
                const dark = box => {
                    const left = Math.max(0, Math.floor((box.left - origin.left) * canvas.width / origin.width));
                    const top = Math.max(0, Math.floor((box.top - origin.top) * canvas.height / origin.height));
                    const width = Math.min(canvas.width - left, Math.ceil(box.width * canvas.width / origin.width));
                    const height = Math.min(canvas.height - top, Math.ceil(box.height * canvas.height / origin.height));
                    const data = pixels.getImageData(left, top, width, height).data;
                    let count = 0;
                    for (let offset = 0; offset < data.length; offset += 4)
                        if (data[offset] < 150 && data[offset + 1] < 150 && data[offset + 2] < 150) count++;
                    return count;
                };
                const spanBoxes = spans.filter(span => span.firstChild).map(span => {
                    const range = document.createRange();
                    range.selectNodeContents(span);
                    return {box: range.getBoundingClientRect(), glyphs: Boolean(span.textContent.trim())};
                });
                const rectangles = hits.map(hit => hit.getBoundingClientRect());
                const located = rectangles.map(box => spanBoxes.find(({box: word}) =>
                    box.left >= word.left - 1 && box.right <= word.right + 1 &&
                    box.top >= word.top - 1 && box.bottom <= word.bottom + 1));
                const selection = window.getSelection();
                const range = document.createRange();
                range.setStart(matched[0].firstChild, 0);
                range.setEnd(matched[1].firstChild, matched[1].textContent.length);
                selection.removeAllRanges(); selection.addRange(range);
                const selected = selection.toString();
                selection.removeAllRanges();
                return {count: hits.length, positioned: located.every(Boolean),
                        dark: rectangles.filter((_, index) => located[index]?.glyphs).map(dark), selected,
                        size: [origin.width, origin.height],
                        pointer_events: getComputedStyle(hits[0]).pointerEvents};
            }""")

        try:
            page.goto(base, wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            started = time.monotonic()
            open_file(LARGE)
            if settings['baseline']:
                expect(viewer.locator('.textLayer')).to_contain_text('SeriesFlow page 1')
                assert page.get_by_test_id('pdf-rotate').count() == 0
                assert highlights.count() == 0 and page.get_by_role('spinbutton', name='PDF 页码').count() == 0
                report.update(passed=True, missing_rotation_jump_and_highlight=True)
                return
            visible_page(1, 'SeriesFlow page 1')
            initial = list(requests)
            heads = [item for item in initial if item['method'] == 'HEAD']
            ranges = [item for item in initial if item['method'] == 'GET']
            first_bytes = sum(item['bytes'] for item in ranges)
            assert len(heads) == 1 and heads[0]['status'] == 200 and heads[0]['bytes'] == settings['size']
            assert len(ranges) > 0 and 0 < first_bytes < settings['size'] * .12, (initial, first_bytes)
            assert all(item['status'] == 206 and item['range'].endswith('/' + str(settings['size']))
                       and item['version'] == heads[0]['version'] for item in ranges)
            report['initial'] = {'elapsed_ms': round((time.monotonic() - started) * 1000),
                                 'bytes': first_bytes, 'file_bytes': settings['size'], 'requests': initial}

            hold_next_range()
            search.fill('SeriesFlow')
            search_run.click()
            await_range()
            page.get_by_test_id('pdf-search-cancel').click()
            expect(notice).to_have_text('已取消搜索')
            (root / 'release-range').touch()
            page.wait_for_timeout(400)
            expect(results).to_have_count(0)
            expect(highlights).to_have_count(0)
            expect(notice).to_have_text('已取消搜索')
            report['cancelled_while_waiting_for_range'] = True

            search_run.click()
            expect(notice).to_have_text('找到 56 个匹配页，共 56 处')
            expect(results).to_have_count(50)
            page.get_by_role('button', name='更多匹配页', exact=True).click()
            expect(results).to_have_count(PAGES)
            expect(results.last).to_have_attribute('data-page', str(PAGES))

            search.fill('  SPLIT     token ')
            search.press('Enter')
            expect(notice).to_have_text('找到 1 个匹配页，共 2 处')
            expect(results).to_have_count(1)
            results.click()
            visible_page(PAGES, 'SeriesFlow page 56')
            expect(results).to_have_attribute('aria-current', 'page')
            original = assert_overlay()
            assert original['count'] >= 4 and original['positioned'], original
            assert len(original['dark']) == 4 and min(original['dark']) > 10, original
            assert re.sub(r'\s+', ' ', original['selected']).strip() == 'Split token', original
            assert original['pointer_events'] == 'none'
            page.screenshot(path=str(root / 'pdf-native-rotation-search.png'))
            page.get_by_test_id('pdf-rotate').click()
            expect(viewer).to_have_attribute('data-rotation', '90')
            visible_page(PAGES, 'SeriesFlow page 56')
            rotated = assert_overlay()
            assert rotated['size'] == list(reversed(original['size'])), (original, rotated)
            assert rotated['count'] == original['count'] and rotated['positioned'], rotated
            assert len(rotated['dark']) == 4 and min(rotated['dark']) > 10, rotated
            page.screenshot(path=str(root / 'pdf-added-rotation-search.png'))
            report['overlay'] = {'native_rotation': original, 'additional_rotation': rotated}

            page.get_by_test_id('pdf-zoom-in').click()
            expect(viewer).to_have_attribute('data-scale', '1.4')
            jump = page.get_by_role('spinbutton', name='PDF 页码')
            jump.fill('23')
            jump.press('Enter')
            visible_page(23, 'SeriesFlow page 23')
            page.get_by_test_id('pdf-prev').click()
            visible_page(22, 'SeriesFlow page 22')
            page.get_by_test_id('pdf-next').click()
            visible_page(23, 'SeriesFlow page 23')
            page.get_by_test_id('pdf-outline-item').click()
            visible_page(PAGES, 'SeriesFlow page 56')
            page.reload(wait_until='domcontentloaded')
            open_file(LARGE)
            visible_page(PAGES, 'SeriesFlow page 56')
            expect(viewer).to_have_attribute('data-scale', '1.4')
            expect(viewer).to_have_attribute('data-rotation', '90')
            report['pagination_outline_zoom_rotation_persisted'] = True

            hold_next_range()
            search.fill('SeriesFlow')
            search_run.click()
            await_range()
            open_file(OTHER)
            visible_page(1, 'ReplacementOnly')
            (root / 'release-range').touch()
            page.wait_for_timeout(350)
            assert 'net::ERR_ABORTED' in failed, failed
            expect(results).to_have_count(0)
            expect(highlights).to_have_count(0)
            expect(viewer).to_have_attribute('data-rotation', '0')
            report['document_switch_cancelled_range'] = True
            assert not report['page_errors'] and not report['inference_requests'], report
            assert all(item['status'] == 206 for item in requests if item['method'] == 'GET'), requests
            report['passed'] = True
        finally:
            (root / 'release-range').touch()
            if not report['passed']:
                page.screenshot(path=str(root / 'pdf-reader-failure.png'))
            save(root / 'pdf-browser.json', report)
            edge.close()


def upload(client, api, source, target):
    with source.open('rb') as content:
        digest = hashlib.file_digest(content, 'sha256').hexdigest()
        size = source.stat().st_size
        path = api.prefix + '/uploads'
        session = api.call('POST', path, {'path': target, 'size': size, 'sha256': digest})
        route = '/api/v1' + path + '/' + session['id']
        content.seek(0)
        for offset in range(0, size, 8 * 1024 * 1024):
            body = content.read(8 * 1024 * 1024)
            reply = client.put(route, params={'offset': offset}, content=body)
            reply.raise_for_status()
        completed = api.call('POST', path + '/' + session['id'] + ':complete')
        assert completed['ok'] and completed['sha256'] == digest, completed
        return {'bytes': size, 'sha256': digest}


def verify(args):
    import httpx
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    api = IsolatedServer('pdf-reader-e15', app='pdf_reader_fixture:app')
    root = api.root
    print('PDF_READER_EVIDENCE', root, flush=True)
    (root / 'pdf_reader_fixture.py').write_text(f'DIST={str(dist)!r}\nLARGE={LARGE!r}\n' + APP, encoding='utf8')
    browser_temp = root / 'edge-tmp'
    browser_temp.mkdir()
    source = root / 'source.pdf'
    other = root / 'replacement.pdf'
    make_pdf(source, PAGES)
    make_pdf(other, 1)
    report = {'passed': False, 'baseline': args.baseline}
    try:
        with api:
            api.create_case(CASE)
            route = api.prefix + '/raw?' + urlencode({'path': LARGE})
            with httpx.Client(base_url=api.base, headers={'Authorization': 'Bearer ' + api.token},
                              timeout=60, trust_env=False) as client:
                report['fixture'] = upload(client, api, source, LARGE)
                upload(client, api, other, OTHER)
                size = report['fixture']['bytes']
                assert size > 60 * 1024 * 1024
                probe = client.get('/api/v1' + route, headers={'Range': 'bytes=0-7'})
                assert probe.status_code == 206 and probe.content.startswith(b'%PDF-')
            stranger = api.call('POST', '/auth/register', {'email': f'{uuid4().hex}@example.com',
                                'name': 'Unrelated PDF reader owner', 'password': 'isolated-test-123'})
            api.call('HEAD', route, expect=404, raw=True,
                     extra_headers={'Authorization': 'Bearer ' + stranger['token']})
            api.call('HEAD', route, expect=401, raw=True, extra_headers={'Authorization': ''})
            report['authorization'] = {'other_account': 404, 'unauthenticated': 401}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                           input=json.dumps({'root': str(root), 'base': api.base, 'token': api.token,
                                             'size': size, 'baseline': args.baseline}), check=True,
                           text=True, encoding='utf8', timeout=300, cwd=root,
                           env=dict(os.environ, TEMP=str(browser_temp), TMP=str(browser_temp)))
            report['browser'] = json.loads((root / 'pdf-browser.json').read_text(encoding='utf8'))
            assert report['browser']['passed']
        assert api.proc.poll() is not None
        report['passed'] = True
    finally:
        save(root / 'pdf-reader-result.json', report)
    print('PDF_READER_PASS', root / 'pdf-reader-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--baseline', action='store_true')
        verify(parser.parse_args())
