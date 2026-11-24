"""Check the source index through the authenticated API and the built Monaco editor."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from live_llm_job_repeat_acceptance import main


FILES = {
    'Ref/controlDict': 'writeControl\n    timeS',
    'Ref/0/U': 'boundaryField { inlet { type fixedV',
    'Ref/0/V': 'boundaryField { inlet { type /* keep */ fixedG',
    'Ref/0/copiedInitialField': 'type fixedG',
}


def browser_check(args):
    from playwright.sync_api import expect, sync_playwright

    root = Path(args['root'])
    with sync_playwright() as play:
        browser = play.chromium.launch(channel=os.getenv('VIBEFLOW_TEST_BROWSER_CHANNEL') or None)
        context = browser.new_context(viewport={'width':1280,'height':840}, locale='zh-CN')
        context.add_init_script('if (location.origin === ' + json.dumps(args['base']) +
                                ') localStorage.setItem("vf_token", ' + json.dumps(args['token']) + ');')
        page = context.new_page()
        checks = []

        def row(path):
            return page.locator('[data-path=' + json.dumps(path) + ']')

        def open_file(path):
            pieces = path.split('/')
            for index in range(1, len(pieces)):
                folder = row('/'.join(pieces[:index]))
                if folder.inner_text().startswith('▸'):
                    folder.click()
            tab = page.locator('[data-editor-path=' + json.dumps(path) + ']')
            def fetched(response):
                url = urlsplit(response.url)
                return url.path.endswith('/file') and parse_qs(url.query).get('path') == [path]
            for _ in range(5):
                with page.expect_response(fetched) as reply:
                    row(path).click()
                response = reply.value
                if response.ok:
                    break
                assert response.status == 409 and response.json()['detail']['code'] in {
                    'file_sync_busy','storage_quota_busy','case_busy','validation_busy'}, (path,response.status)
                page.wait_for_timeout(150)
            expect(tab).to_have_attribute('data-active', 'true',timeout=15000)
            page.wait_for_function('(value) => window.monaco?.editor.getModels().at(-1)?.getValue() === value',
                                   arg=FILES[path])

        def suggest(parent, prefix, file_context, *, response_expected=True):
            page.evaluate('''() => {
                const model = window.monaco.editor.getModels().at(-1);
                const editor = window.monaco.editor.getEditors().find((entry) => entry.getModel() === model);
                const lineNumber = model.getLineCount();
                editor.setPosition({lineNumber, column: model.getLineMaxColumn(lineNumber)});
                editor.focus();
            }''')
            def matched(response):
                url = urlsplit(response.url)
                query = parse_qs(url.query)
                return (url.path == '/api/v1/completion/openfoam' and query.get('q') == [prefix]
                        and query.get('parent') == [parent] and query.get('context') == ([file_context] if file_context else None))
            expect_network = page.expect_response if response_expected else page.expect_request
            with expect_network(matched):
                page.keyboard.press('Control+Space')

        def choose(label, suffix=None):
            popup = page.locator('.suggest-widget.visible')
            option = popup.get_by_text(label, exact=True)
            expect(option).to_be_visible()
            expect(option).to_have_count(1)
            page.keyboard.press('Enter')
            page.wait_for_function('(value) => window.monaco.editor.getModels().at(-1).getValue().endsWith(value)',
                                   arg=suffix or label)
            page.keyboard.press('Escape')

        try:
            page.goto(args['base'])
            page.locator('[data-case-name=' + json.dumps(args['case_name']) + ']').click()
            open_file('Ref/controlDict')
            suggest('writeControl','timeS','controlDict')
            choose('timeStep')
            checks.append('controlDict multiline key replaced only the typed value prefix')
            open_file('Ref/0/U')
            suggest('type','fixedV','U')
            choose('fixedValue')
            open_file('Ref/0/V')
            suggest('type','fixedG','V')
            candidate = page.locator('.suggest-widget.visible .monaco-list-row').filter(has_text='fixedGradient')
            expect(candidate).to_be_visible()
            page.keyboard.press('Control+Space')
            expect(page.locator('.suggest-details p.docs')).to_have_text(re.compile(r'\$WM_PROJECT_DIR/.+/0/V$'))
            page.screenshot(path=root/'foam-context.png')
            page.keyboard.press('Control+Space')
            choose('fixedGradient','/* keep */ fixedGradient')
            checks.append('field context chooses the value, shows its source and preserves the comment')

            open_file('Ref/0/copiedInitialField')
            with page.expect_response(lambda response: '/completion/openfoam?' in response.url and
                                      parse_qs(urlsplit(response.url).query).get('context') is None):
                suggest('type','fixedG','copiedInitialField')
            choose('fixedGradient')
            checks.append('unknown file context falls back to the same dictionary key')

            page.reload()
            page.locator('[data-case-name=' + json.dumps(args['case_name']) + ']').click()
            open_file('Ref/controlDict')
            page.route('**/api/v1/completion/openfoam?*', lambda route:route.abort())
            suggest('writeControl','timeS','controlDict',response_expected=False)
            choose('timeStep')
            page.unroute('**/api/v1/completion/openfoam?*')
            checks.append('cancelled HTTP request falls back to the local value list')
        except Exception:
            page.screenshot(path=root/'foam-browser-failure.png')
            raise
        finally:
            (root/'foam-browser.json').write_text(json.dumps({'checks':checks,'browser':browser.version},indent=2))
            browser.close()


def scenario(api, **_):
    endpoint = '/completion/openfoam'
    index_path = api.root/'active-completion.json'
    with httpx.Client(base_url=api.base + '/api/v1', trust_env=False, timeout=20) as unauthenticated:
        assert unauthenticated.get(endpoint).status_code == 401
    assert api.call('GET',endpoint) == {'ready':False,'items':[]}
    index_path.write_text('{',encoding='utf8')
    assert api.call('GET',endpoint) == {'ready':False,'items':[]}
    shutil.copyfile(os.environ['VIBEFLOW_TEST_FOAM_INDEX'], index_path)

    def complete(**parameters):
        return api.call('GET', endpoint + '?' + urlencode(parameters))

    with ThreadPoolExecutor(4) as pool:
        workers = [pool.submit(complete, q='volScalarField') for _ in range(4)]
        results = [call.result(timeout=20) for call in workers]
    assert all(result == results[0] for result in results)
    index = results[0]
    assert index['ready'] and index['total'] == 31542
    assert all(len(index[field]) == 64 for field in ('baseline_sha256','generator_sha256','index_sha256'))
    assert index['items'][0]['label'] == 'volScalarField'
    guest = api.call('POST','/auth/register',{'name':'Completion guest','email':'completion-guest@example.com',
                                            'password':'isolated-completion-123'})
    assert api.call('GET',endpoint+'?q=volScalarField',extra_headers={'Authorization':'Bearer '+guest['token']}) == index
    for parameters in ('limit=201','q='+'x'*65,'parent='+'p'*65,'context='+'c'*65):
        api.call('GET',endpoint+'?'+parameters,expect=422)
    kinds = {'cpp14Keyword':'auto','cppPreprocessorDirective':'#define','foamFileClass':'volVectorField',
             'commandLineToken':'--2D','wmakePathVariable':'FOAM_LIBBIN','wmakeVariable':'FOAM_SRC'}
    for kind, label in kinds.items():
        assert label in {entry['label'] for entry in complete(q=label,kind=kind)['items']}, (kind,label)
    field = complete(q='fixedGradient',parent='type',kind='dictionaryValue',context='V')['items'][0]
    assert field['sourceExamples'][0].endswith('/0/V')
    assert not complete(q='fixedGradient',parent='type',kind='dictionaryValue',context='U')['items']
    assert not complete(q='fixedGradient',parent='unknown',kind='dictionaryValue',context='V')['items']
    assert 'uniform' not in {item['label'] for item in complete(q='uniform',parent='type',context='U')['items']}
    for path, text in FILES.items():
        api.put(path,text)
    case = api.call('GET',api.prefix)
    arguments = {'base':api.base,'root':str(api.root),'case_name':case['name'],'token':api.token}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], str(Path(__file__).resolve()), '--browser'],
                   input=json.dumps(arguments), text=True, timeout=160, check=True)
    report = {'index_hash':index['index_sha256'],'baseline_hash':index['baseline_sha256'],
              'official_context_source':field['sourceExamples'],'labels':index['total'],'kinds':list(kinds),
              'missing_and_corrupt_recovery':True, 'anonymous_denied':True, 'second_account_same_public_index':True,
              'concurrent_cold_readers':len(results), 'browser':json.loads((api.root/'foam-browser.json').read_text())}
    (api.root/'foam-completion.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser_check(json.load(sys.stdin))
    else:
        bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
        assert (bundle/'index.html').is_file()
        fixture = '\nfrom app.services import completion\ncompletion._INDEX_PATH = root / "active-completion.json"\n'
        fixture += '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(bundle))+', html=True))\n'
        asyncio.run(main(selected_override=scenario, fixture_override=fixture, store_volume_max=5))
