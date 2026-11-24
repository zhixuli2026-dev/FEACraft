"""Measure the shipped FormulaNet model through the production formula panel in Edge."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


CATEGORIES = {
    'notation': ('energy', 'linear', 'subscripts', 'greek'),
    'fractions_and_radicals': ('fraction', 'root', 'fraction_nested'),
    'calculus_and_operators': ('series', 'calculus', 'limit', 'gradient', 'partial'),
    'structured': ('matrix', 'piecewise'),
}


def tokens(latex):
    latex = re.sub(r'\\(?:left|right)\b|\\[!,;: ]', '', latex)
    # Preserve the boundary between a named control word and the next letter.
    parts = re.findall(r'\\[A-Za-z]+|\\[^\s]|[^\s]', latex)
    normalized = []
    offset = 0
    while offset < len(parts):
        if (parts[offset] in ('_', '^') and offset + 3 < len(parts)
                and parts[offset + 1] == '{' and parts[offset + 3] == '}'
                and parts[offset + 2] not in ('{', '}', '_', '^')):
            normalized.extend((parts[offset], parts[offset + 2]))
            offset += 4
        else:
            normalized.append(parts[offset])
            offset += 1
    return normalized


def distance(expected, actual):
    previous = list(range(len(actual) + 1))
    for i, wanted in enumerate(expected, 1):
        current = [i]
        for j, observed in enumerate(actual, 1):
            current.append(min(previous[j] + 1, current[-1] + 1,
                               previous[j - 1] + (wanted != observed)))
        previous = current
    return previous[-1]


def score(rows):
    edits = sum(row['token_edits'] for row in rows)
    total = sum(row['reference_tokens'] for row in rows)
    return {'samples': len(rows), 'exact': sum(row['exact'] for row in rows),
            'token_edits': edits, 'reference_tokens': total, 'token_error_rate': round(edits / total, 4)}


def exercise(config):
    from playwright.sync_api import expect, sync_playwright

    samples = Path(config['samples'])
    root = Path(config['root'])
    truth = json.loads((samples / 'truth.json').read_text(encoding='utf-8'))
    assert {row['name'] for row in truth} == {name for members in CATEGORIES.values() for name in members}
    rows, assets, egress, events = [], {}, [], []
    proof = {'passed': False, 'samples': rows, 'local_model_assets': assets, 'external_requests': egress,
             'actual_formulanet_results': 0, 'actual_llm_calls': 0, 'simulated_llm_calls': 0}
    with sync_playwright() as play:
        browser = play.chromium.launch(channel='msedge')
        context = browser.new_context(viewport={'width': 1580, 'height': 900}, locale='zh-CN',
                                      permissions=['clipboard-read', 'clipboard-write'])
        context.add_init_script('if (location.origin === ' + json.dumps(config['base'])
                                + ') localStorage.setItem("vf_token", ' + json.dumps(config['token']) + ');')

        def local_only(route):
            url = route.request.url
            if url.startswith((config['base'] + '/', 'blob:', 'data:')):
                route.continue_()
            else:
                egress.append(url.split('?', 1)[0])
                route.abort()

        context.route('**/*', local_only)
        page = context.new_page()
        page.on('response', lambda response: assets.update({response.url.split('/texo/', 1)[1]: response.status})
                if '/texo/' in response.url else None)
        page.on('console', lambda message: events.append(message.text.split(':', 3)[1])
                if message.text.startswith('VIBEFLOW_TEXO:') else None)
        proof['browser'] = browser.version
        try:
            page.goto(config['base'], wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(config['case_name']) + ']').click(timeout=25000)
            page.locator('.dv-tab').get_by_text('公式识别', exact=True).click(timeout=25000)
            worker = page.locator('iframe[title="texo-worker"]')
            expect(worker).to_be_attached()
            frame = worker.element_handle().content_frame()
            frame.wait_for_function("typeof window.vfTexoRecognize === 'function'", timeout=20000)
            view = worker.locator('..')
            picker = view.locator('input[type=file]')
            result = view.get_by_role('textbox', name='识别结果')
            status = view.get_by_test_id('texo-ocr-status')
            run = view.get_by_role('button', name='识别公式', exact=True)

            def recognize(input_file):
                terminal = len([event for event in events if event in ('result', 'error')])
                picker.set_input_files(input_file)
                expect(status).to_contain_text('图片已就绪')
                started = time.monotonic()
                run.click()
                expect(status).to_have_text(re.compile('^(识别完成|识别失败：|调用失败：|未识别到公式)'), timeout=120000)
                elapsed = round((time.monotonic() - started) * 1000)
                expect(run).to_be_enabled()
                assert len([event for event in events if event in ('result', 'error')]) == terminal + 1, events
                return result.input_value() if result.count() else '', status.inner_text(), elapsed

            # First inference loads the local models. The final unseen image runs with the browser offline.
            for index, sample in enumerate(truth):
                offline = index == len(truth) - 1
                context.set_offline(offline)
                png = samples / sample['png']
                observed, result_status, duration_ms = recognize(png)
                expected_tokens, actual_tokens = tokens(sample['latex']), tokens(observed)
                rows.append({**sample, 'sha256': hashlib.sha256(png.read_bytes()).hexdigest(),
                             'observed': observed, 'status': result_status, 'latency_ms': duration_ms,
                             'browser_offline': offline, 'exact': expected_tokens == actual_tokens,
                             'reference_tokens': len(expected_tokens),
                             'token_edits': distance(expected_tokens, actual_tokens)})

            assert rows[-1]['observed'] and rows[-1]['status'] == '识别完成', rows[-1]
            proof['metrics'] = {'overall': score(rows), **{
                category: score([row for row in rows if row['name'] in members])
                for category, members in CATEGORIES.items()}}
            proof['actual_formulanet_results'] = events.count('result')
            assert proof['actual_formulanet_results'] == len(rows), rows
            assert sum(path.endswith('.onnx') and status == 200 for path, status in assets.items()) >= 2, assets
            assert not egress, egress

            context.set_offline(False)
            damaged = {'name': 'damaged.png', 'mimeType': 'image/png', 'buffer': b'damaged raster bytes'}
            observed, failure, _ = recognize(damaged)
            assert not observed and failure.startswith('识别失败：'), failure
            correction = next((row for row in rows if not row['exact'] and row['observed']), rows[0])
            picker.set_input_files(samples / correction['png'])
            expect(status).to_contain_text('缓存命中')
            expect(result).to_have_value(correction['observed'])
            result.fill(correction['latex'])
            view.get_by_role('button', name='复制 LaTeX', exact=True).click()
            page.wait_for_function('expected => navigator.clipboard.readText().then(value => value === expected)',
                                   arg=correction['latex'])
            proof['correction'] = {'sample': correction['name'], 'original': correction['observed'],
                                   'copied': correction['latex'], 'recoverable_failure': failure.splitlines()[0]}
            page.screenshot(path=root / 'manual-correction.png')
            proof['passed'] = True
        finally:
            proof['actual_formulanet_results'] = events.count('result')
            proof['recognizer_errors'] = events.count('error')
            (root / 'texo-accuracy.json').write_text(json.dumps(proof, indent=2, ensure_ascii=False) + '\n',
                                                     encoding='utf-8')
            browser.close()


def main(samples):
    from testsupport import IsolatedServer

    dist = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (dist / 'texo/model/onnx/encoder_model.onnx').is_file()
    api = IsolatedServer('ff-e19-texo-edge', app='texo_fixture:app')
    (api.root / 'texo_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf-8')
    with api:
        case_name = 'FormulaNet accuracy benchmark'
        api.create_case(case_name)
        settings = {'base': api.base, 'root': str(api.root), 'samples': str(Path(samples).resolve(strict=True)),
                    'case_name': case_name, 'token': api.token}
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], '-X', 'utf8', __file__, '--edge'],
                       input=json.dumps(settings), text=True, timeout=360, check=True)
    print('TEXO_ACCURACY', api.root / 'texo-accuracy.json', flush=True)


if __name__ == '__main__':
    exercise(json.load(sys.stdin)) if sys.argv[1:] == ['--edge'] else main(sys.argv[1])
