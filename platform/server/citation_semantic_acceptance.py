"""Verify Semantic Scholar through the real workbench and bounded HTTP protocol checks."""
import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit

from citation_semantic_protocol_smoke import protocol


CASE = 'Live Semantic Scholar citations'
DOI = '10.1145/1330311.1330313'
TEX = 'E-report/latex/semantic.tex'
BIB = 'E-report/latex/refs.bib'
TEXT = 'First FIRST_SLOT\nAgain NEXT_SLOT\n'
LIBRARY = '% Keep this comment and the existing source.\n@book{existing, title = {Notes}}\n'


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    base, root = settings['base'], Path(settings['root'])
    auth = {'Authorization': 'Bearer ' + settings['token']}
    file_url = base + '/api/v1' + settings['prefix'] + '/file'
    result = {'passed': False, 'model_requests': [], 'bib_writes': 0}
    with sync_playwright() as driver:
        edge = driver.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1680, 'height': 970}, locale='zh-CN')
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(40_000)
        expect.set_options(timeout=35_000)

        def observe(request):
            if request.method == 'POST' and urlsplit(request.url).path.endswith(('/messages', '/turns', '/goal:run')):
                result['model_requests'].append(urlsplit(request.url).path)
            if request.method == 'PUT' and request.url == file_url and request.post_data_json.get('path') == BIB:
                result['bib_writes'] += 1

        def content(path):
            response = context.request.get(file_url + '?' + urlencode({'path': path}), headers=auth)
            assert response.ok, response.status
            return response.json()['content']

        def search(query):
            page.get_by_test_id('cite-query').fill(query)
            with page.expect_response(lambda reply: urlsplit(reply.url).path == '/api/v1/cite/search', timeout=65_000) as found:
                page.get_by_test_id('cite-search-run').click()
            return found.value

        def select(marker):
            page.evaluate("""marker => {
                const editor = window.monaco.editor.getEditors().at(-1), model = editor.getModel();
                const offset = model.getValue().indexOf(marker);
                if (offset < 0) throw new Error('Citation slot disappeared');
                const start = model.getPositionAt(offset), end = model.getPositionAt(offset + marker.length);
                editor.setSelection({startLineNumber:start.lineNumber,startColumn:start.column,
                    endLineNumber:end.lineNumber,endColumn:end.column});
            }""", marker)

        def write(control, path, submitted):
            deadline = time.monotonic() + 12
            while True:
                with page.expect_response(lambda reply: reply.request.method == 'PUT' and reply.url == file_url
                                          and reply.request.post_data_json.get('path') == path) as applied:
                    control.click()
                response = applied.value
                detail = response.json().get('detail') if response.status == 409 else None
                if not isinstance(detail, dict) or detail.get('code') != 'storage_quota_busy':
                    assert response.ok, (response.status, response.text()[:200])
                    assert response.request.post_data_json['expected_sha256'] == sha256(submitted.encode()).hexdigest()
                    return
                assert time.monotonic() < deadline
                page.wait_for_timeout(300)

        try:
            page.on('request', observe)
            page.goto(base, wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^公式调色板$')).click()
            page.get_by_test_id('cite-source').select_option('semanticscholar')
            found = search('https://doi.org/10.1145%2F1330311.1330313')
            result['doi_http'] = found.status
            assert found.ok, (found.status, found.text()[:250])
            item, = found.json()['items']
            assert item['id'] == 'doi:' + DOI and item['source'] == 'semanticscholar' and item['title']
            key = item['key_hint']
            row = page.locator('[data-citation-id=' + json.dumps(item['id']) + ']')
            expect(row.get_by_test_id('cite-preview')).to_contain_text(DOI)
            expect(row).to_contain_text(item['title'])
            for directory in ('E-report', 'E-report/latex'):
                target = page.locator('[data-path=' + json.dumps(directory) + ']')
                if target.get_attribute('aria-expanded') == 'false':
                    target.click()
            page.locator('[data-path=' + json.dumps(TEX) + ']').click()
            page.wait_for_function('value => window.monaco.editor.getEditors().at(-1)?.getValue() === value', arg=TEXT)
            select('FIRST_SLOT')
            button = row.get_by_test_id('cite-add')
            write(button, BIB, LIBRARY)
            expect(page.get_by_test_id('cite-feedback')).to_contain_text(key)
            current = content(BIB)
            assert current.startswith(LIBRARY) and current.count('doi = {' + DOI + '}') == 1
            writes = result['bib_writes']
            select('NEXT_SLOT')
            button.click()
            expected = TEXT.replace('FIRST_SLOT', '\\cite{' + key + '}').replace('NEXT_SLOT', '\\cite{' + key + '}')
            page.wait_for_function('value => window.monaco.editor.getEditors().at(-1)?.getValue() === value', arg=expected)
            assert result['bib_writes'] == writes and content(BIB) == current
            write(page.get_by_role('button', name='保存 (Ctrl+S)', exact=True), TEX, TEXT)
            assert content(TEX) == expected
            page.screenshot(path=str(root / 'semantic-reference-workbench.png'))
            result['doi'] = {'id': item['id'], 'title': item['title'], 'year': item['year'], 'authors': item['authors'], 'key': key}
            result['bibliography_sha256'] = sha256(current.encode()).hexdigest()
            result['editor_sha256'] = sha256(expected.encode()).hexdigest()
            terms = search(settings['keyword'])
            result['keyword'] = {'http': terms.status, 'query': settings['keyword']}
            if terms.ok:
                result['keyword']['items'] = terms.json()['items']
            else:
                assert terms.status in (502, 503)
                result['keyword'].update(detail=terms.json()['detail'], retry_after=terms.headers.get('retry-after'))
                expect(page.get_by_test_id('cite-error')).to_contain_text('Semantic Scholar')
            assert content(BIB) == current and content(TEX) == expected
            assert not result['model_requests']
            result['passed'] = True
        finally:
            write_json(root / 'semantic-browser.json', result)
            if not result['passed']:
                page.screenshot(path=str(root / 'semantic-failure.png'))
            edge.close()


def verify(args):
    from testsupport import IsolatedServer

    checks = asyncio.run(protocol())
    api = IsolatedServer('semantic-citation-e07', app='semantic_fixture:app')
    root = api.root
    dist = args.dist.resolve(strict=True)
    scratch = root / 'native-tmp'
    scratch.mkdir()
    (root / 'semantic_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf8')
    env = dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(scratch), TMP=str(scratch))
    report = {'passed': False, 'model_calls': 0, 'http_protocol_checks': checks}
    print('SEMANTIC_CITATION_EVIDENCE', root, flush=True)
    try:
        with api:
            api.call('GET', '/cite/search?' + urlencode({'q': DOI, 'source': 'semanticscholar'}), expect=401)
            api.create_case(CASE)
            api.put(TEX, TEXT)
            api.put(BIB, LIBRARY)
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                input=json.dumps({'root': str(root), 'base': api.base, 'prefix': api.prefix, 'token': api.token,
                                  'keyword': args.keyword}), encoding='utf8', check=True, cwd=root, env=env, timeout=190)
            report['browser'] = json.loads((root / 'semantic-browser.json').read_text(encoding='utf8'))
            assert report['browser']['passed']
            for path, field in ((BIB, 'bibliography_sha256'), (TEX, 'editor_sha256')):
                data = api.call('GET', api.prefix + '/raw?' + urlencode({'path': path}), raw=True)
                assert sha256(data).hexdigest() == report['browser'][field]
            other = api.call('POST', '/auth/register', {'email':'semantic-other@example.com','name':'Unrelated citation owner',
                                                        'password':'isolated-test-123'})
            api.call('GET', api.prefix + '/file?' + urlencode({'path': BIB}), expect=404,
                     extra_headers={'Authorization':'Bearer ' + other['token']})
            report['foreign_account_http'] = 404
        report['isolated_api_exited'] = api.proc.poll() is not None
        assert report['isolated_api_exited']
        report['passed'] = True
    finally:
        write_json(root / 'semantic-citation-result.json', report)
    print('SEMANTIC_CITATION_PASS', root / 'semantic-citation-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--keyword', default='OpenFOAM')
        verify(parser.parse_args())
