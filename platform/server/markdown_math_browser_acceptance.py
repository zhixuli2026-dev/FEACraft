"""Verify authenticated Markdown preview and local formula compatibility in Edge."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit


CASE = 'Markdown formula compatibility'
STANDARD = 'Ref/formulas.md'
COMPATIBILITY = 'Ref/compatibility.md'
OTHER = 'Ref/new-document.md'
FAST = r'''# 基础公式和普通 Markdown

行内 $x^2+1$ 与 \(\alpha+\beta\)。

$$\int_0^1 t\,dt=\frac12$$

\[\sqrt{a^2+b^2}\]

|列|值|
|---|---|
|温度|23|

价格为 \$5，原样显示 `$code$`。
'''
RICH = r'''# 兼容公式笔记

已标记的质量守恒式 \eqref{vf:mass}。

\begin{align}
  \dot m &= \rho A U \tag{M42}\label{vf:mass}
\end{align}

\begin{numcases}{g(x)=}
  x^2 & if $x > 0$ \\
  0 & otherwise
\end{numcases}

$$\text{flow\enspace\textbf{Bold}}=\frac{Q}{A}$$

$$\newcommand{\vfunit}{\mathrm{kg}}$$

质量单位为 $3\,\vfunit$。

<math xmlns="http://www.w3.org/1998/Math/MathML"><mfrac><mn>7</mn><mn>13</mn></mfrac></math>

下面原样保留 `$source$` 和转义的 \$7。

```tex
\begin{numcases}{disabled}
$not-typeset$
\end{numcases}
```

对未知命令 $\vfUnknownMacro$ 保留可读源码。

[危险地址](javascript:window.flagged=1)

<script>window.flagged=1</script>
<img src="" onerror="window.flagged=1" alt="事件属性对照" />

$$\href{javascript:window.flagged=1}{unsafe}$$
'''
FRESH = r'''# 独立文档

另一文件的标记引用 \eqref{vf:mass}。

\[\begin{matrix}a&b\\c&d\end{matrix}\]
'''

STATIC = '''
from pathlib import Path
import json, os
from fastapi.staticfiles import StaticFiles
from app.main import app
from app.services.process_identity_native import observe
(Path(__file__).parent/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')
app.mount('/', StaticFiles(directory=DIST, html=True))
'''


def write_receipt(root, name, data):
    (root/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    result = {'passed': False, 'baseline': settings['baseline'], 'page_errors': [], 'external_requests': []}
    with sync_playwright() as toolkit:
        edge = toolkit.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1480, 'height': 900}, locale='zh-CN')
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(30_000)
        expect.set_options(timeout=25_000)
        page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
        assets = []

        def observe(request):
            url = urlsplit(request.url)
            if url.scheme in ('http', 'https') and url.hostname not in ('127.0.0.1', 'localhost'):
                result['external_requests'].append(url.hostname)
            if 'markdownMathCompatibility-' in url.path:
                assets.append(url.path)

        page.on('request', observe)
        preview = page.get_by_test_id('markdown-preview')
        toggle = page.get_by_test_id('markdown-preview-toggle')

        def show(path, heading):
            page.locator('[data-path=' + json.dumps(path) + ']').click()
            expect(page.locator('[data-editor-path=' + json.dumps(path) + ']')).to_have_attribute('data-active', 'true')
            if not preview.is_visible():
                toggle.click()
            expect(preview.get_by_role('heading', name=heading)).to_be_visible()

        try:
            page.goto(base)
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            folder = page.locator('[data-path="Ref"]')
            expect(folder).to_be_visible()
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            show(STANDARD, '基础公式和普通 Markdown')
            expect(preview.locator('.katex')).to_have_count(4)
            expect(preview.locator('table')).to_contain_text('23')
            expect(preview.get_by_text('$code$', exact=True)).to_be_visible()
            expect(preview).to_contain_text('$5')
            assert not assets
            result['standard_katex_formulas'] = 4
            show(COMPATIBILITY, '兼容公式笔记')
            if settings['baseline']:
                expect(preview).to_contain_text(r'\begin{numcases}')
                expect(preview.locator('mjx-container')).to_have_count(0)
                result['original_numbered_cases_missing'] = True
                result['passed'] = True
                page.screenshot(path=str(root/'markdown-existing-preview.png'))
                return

            expect(preview).to_have_attribute('aria-busy', 'false')
            expect(preview.locator('mjx-container').first).to_be_visible()
            math = preview.evaluate('''root => {
              const formulas = [...root.querySelectorAll('mjx-container')];
              return formulas.map(node => ({
                value: node.querySelector('mjx-assistive-mml math')?.textContent ?? '',
                rows: node.querySelectorAll('mjx-assistive-mml mtr, mjx-assistive-mml mlabeledtr').length,
                svgPaths: node.querySelectorAll('svg path').length,
              }));
            }''')
            piecewise, = [row for row in math if 'otherwise' in row['value']]
            assert piecewise['rows'] >= 2 and piecewise['svgPaths'] > 0, piecewise
            assert '\\empheq' not in piecewise['value'] and '{' in piecewise['value'], piecewise
            assert sum('M42' in row['value'] for row in math) >= 2, math
            assert any('Bold' in row['value'] and row['svgPaths'] > 0 for row in math), math
            assert any('kg' in row['value'] and '3' in row['value'] for row in math), math
            assert any(row['value'] == '713' and row['svgPaths'] > 0 for row in math), math
            assert any('vfUnknownMacro' in row['value'] for row in math), math
            result['actual_math'] = math
            expect(preview.locator('pre code')).to_contain_text('$not-typeset$')
            expect(preview.locator('pre mjx-container')).to_have_count(0)
            expect(preview.get_by_text('$source$', exact=True)).to_be_visible()
            expect(preview).to_contain_text('$7')
            assert not page.evaluate('window.flagged')
            assert not preview.locator('[onerror], [href^="javascript:"], script').count()
            assert len(assets) == 1, assets
            result['local_compatibility_assets'] = assets
            page.screenshot(path=str(root/'markdown-compatibility.png'))

            show(OTHER, '独立文档')
            expect(preview).to_have_attribute('aria-busy', 'false')
            expect(preview.locator('mjx-container').first).to_be_visible()
            assert not any('M42' in text for text in preview.locator('mjx-assistive-mml').all_text_contents())
            result['isolated_equation_scope'] = True
            toggle.click()
            expect(preview).to_have_count(0)
            toggle.click()
            expect(preview.get_by_role('heading', name='独立文档')).to_be_visible()
            expect(preview.locator('mjx-container').first).to_be_visible()
            assert len(assets) == 1 and not result['external_requests'] and not result['page_errors'], result
            result['passed'] = True
        finally:
            write_receipt(root,'markdown-math-browser.json',result)
            if not result['passed']:
                page.screenshot(path=str(root/'markdown-failure.png'), timeout=4000)
            edge.close()


def run(args):
    from app.services.process_identity_native import probe
    from testsupport import IsolatedServer

    api = IsolatedServer('markdown-math-browser', app='markdown_fixture:app', access_log=True)
    root = api.root
    print('MARKDOWN_MATH_EVIDENCE',root,flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    (root/'markdown_fixture.py').write_text(f'DIST = {str(dist)!r}\n'+STATIC,encoding='utf8')
    temp = root/'browser-tmp'
    temp.mkdir()
    receipt = {'passed': False, 'baseline': args.baseline, 'model_calls': 0, 'mock_llm_calls': 0}
    try:
        with api:
            api.create_case(CASE)
            prefix = api.prefix
            for path, content in [(STANDARD, FAST), (COMPATIBILITY, RICH), (OTHER, FRESH)]:
                api.put(path, content)
            data = {'root': str(root), 'base': api.base, 'token': api.token, 'baseline': args.baseline}
            subprocess.run([str(args.browser_python.resolve(strict=True)),'-B','-X','utf8',str(Path(__file__)),'--browser'],
                input=json.dumps(data),text=True,encoding='utf8',check=True,timeout=195,
                env=dict(os.environ,TMP=str(temp),TEMP=str(temp)))
            receipt['browser'] = json.loads((root/'markdown-math-browser.json').read_text(encoding='utf8'))
            assert api.call('GET',prefix+'/file?'+urlencode({'path':COMPATIBILITY}))['content'] == RICH
            identity = json.loads((root/'api-birth.json').read_text(encoding='utf8'))
            api.create_case('Foreign Markdown visitor')
            api.call('GET',prefix+'/file?'+urlencode({'path':COMPATIBILITY}),expect=404)
            api.token = None
            api.call('GET',prefix+'/file?'+urlencode({'path':COMPATIBILITY}),expect=401)
            receipt['outside_access'] = [404,401]
        assert api.proc.poll() is not None
        api.proc = None
        deadline = time.monotonic() + 10
        while probe(identity)['state'] != 'exited' and time.monotonic() < deadline:
            time.sleep(.08)
        assert probe(identity)['state'] == 'exited'
        with socket.socket() as listener:
            listener.settimeout(.3)
            assert listener.connect_ex(('127.0.0.1',api.port)) != 0
        receipt['api_exited'] = receipt['passed'] = True
    finally:
        write_receipt(root,'markdown-math-result.json',receipt)
    print('MARKDOWN_MATH_PASS',root,flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', required=True, type=Path)
        parser.add_argument('--browser-python', required=True, type=Path)
        parser.add_argument('--baseline', action='store_true')
        run(parser.parse_args())
