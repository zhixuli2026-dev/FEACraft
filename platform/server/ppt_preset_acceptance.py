"""Build every DrawingML preset through the real worker and verify editability and rendering."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode


def check_browser(config):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch(channel='msedge')
        context = browser.new_context(viewport={'width': 1440, 'height': 920}, locale='zh-CN', accept_downloads=True)
        page = context.new_page()
        page.add_init_script('if (location.origin === ' + json.dumps(config['base']) + ') localStorage.setItem(' +
                             '"vf_token", ' + json.dumps(config['token']) + ');')
        page.goto(config['base'], wait_until='domcontentloaded')
        page.locator('[data-case-name=' + json.dumps(config['case_name']) + ']').click(timeout=25000)
        page.locator('.dv-tab:visible').filter(has_text=re.compile('^caseDict$')).click()
        editor = page.get_by_test_id('ppt-editor')
        editor.get_by_text('添加原生图形', exact=True).click()
        choice = editor.get_by_role('combobox', name='PPT 图形预设')
        expect(choice.locator('optgroup option')).to_have_count(187)
        choice.select_option('gear9')
        editor.get_by_label('填充').fill('#bf5c21')
        editor.get_by_label('描边').fill('#1f476b')
        editor.get_by_text('左边距 %', exact=True).locator('input').fill('95')
        expect(editor.get_by_test_id('ppt-build')).to_be_disabled()
        editor.get_by_text('左边距 %', exact=True).locator('input').fill('75')
        editor.get_by_test_id('ppt-title').fill('Editable native gear')
        editor.get_by_test_id('ppt-output').fill('E-report/build/browser-shape.pptx')
        root = Path(config['root'])
        editor.screenshot(path=root / 'preset-controls.png')
        editor.get_by_test_id('ppt-build').click()
        expect(editor.get_by_test_id('ppt-status')).to_contain_text('PPTX 已生成', timeout=90000)
        with page.expect_download() as download:
            editor.get_by_test_id('ppt-download').click()
        download.value.save_as(root / 'browser-shape.pptx')
        browser.close()


def geometry_proof(root, slides):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from app.services.ppt_builder import ppt_geometry

    path = root / 'ppt-presets.pptx'
    deck = Presentation(str(path))
    assert len(deck.slides) == len(slides)
    exported = []
    for page, (slide, wanted) in enumerate(zip(deck.slides, slides), 1):
        for position, entry in enumerate(wanted, 1):
            target = next(shape for shape in slide.shapes if shape.name == f'VibeFlowPreset{position}')
            assert ppt_geometry(target) == entry['preset'], entry
            shape_type = target.shape_type
            assert shape_type == (MSO_SHAPE_TYPE.LINE if entry['kind'] == 'connector' else MSO_SHAPE_TYPE.AUTO_SHAPE), entry
            assert str(target.line.color.rgb) == '164D84', entry
            if entry['kind'] == 'shape':
                assert str(target.fill.fore_color.rgb) == '1B7AC0', entry
            exported.append({**entry, 'page': page, 'xml_name': target.name, 'editable': True,
                             'openxml_shape_type': shape_type.name})
    return exported


def exercise_api():
    from pptx import Presentation
    from app.services.ppt_builder import ppt_geometry
    from testsupport import IsolatedServer

    dist = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    api = IsolatedServer('ff-e21-ppt-presets', app='ppt_fixture:app')
    (api.root / 'ppt_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf-8')
    with api:
        case = 'PPT native geometry acceptance'
        api.create_case(case)
        catalog = api.call('GET', api.prefix + '/ppt/presets')['presets']
        assert len(catalog) == len({item['preset'] for item in catalog}) == 187
        deck_path = 'E-report/build/ppt-presets.pptx'
        manifest, slides = [], []
        for page in range(0, len(catalog), 16):
            entries, shapes = [], []
            for index, item in enumerate(catalog[page:page + 16]):
                row, col = divmod(index, 4)
                shape = {'preset': item['preset'], 'x': 0.06 + col * 0.23, 'y': 0.25 + row * 0.18,
                         'width': 0.15, 'height': 0.11, 'stroke': '#164D84',
                         'fill': '#1B7AC0' if item['kind'] == 'shape' else None}
                shapes.append(shape)
                entries.append({**item, **shape})
            slides.append({'title': f"Native geometry {page + 1}–{page + len(entries)}", 'native_shapes': shapes})
            manifest.append(entries)
        spec = {'output': deck_path, 'title': 'DrawingML native presets', 'slides': slides}
        route = api.prefix + '/ppt:build'
        submitted = api.call('POST', route, spec, expect=(200, 202), extra_headers={'Idempotency-Key': 'ppt-native-presets'})
        job = api.wait_job(submitted['job_id'])
        file = api.call('GET', api.prefix + '/raw?' + urlencode({'path': deck_path}), raw=True)
        (api.root / 'ppt-presets.pptx').write_bytes(file)
        assert api.call('POST', route, spec, expect=(200, 202), extra_headers={'Idempotency-Key': 'ppt-native-presets'})['job_id'] == submitted['job_id']
        invalid = dict(slides=[dict(title='Unknown', native_shapes=[{**slides[0]['native_shapes'][0], 'preset': 'not-a-preset'}])])
        api.call('POST', route, invalid, expect=400)
        records = geometry_proof(api.root, manifest)
        assert len(records) == 187

        settings = {'base': api.base, 'root': str(api.root), 'case_name': case, 'token': api.token}
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], '-X', 'utf8', __file__, '--edge'],
                       input=json.dumps(settings), text=True, timeout=180, check=True)
        browser_deck = Presentation(api.root / 'browser-shape.pptx')
        browser_shape = next(shape for shape in browser_deck.slides[0].shapes if shape.name == 'VibeFlowPreset1')
        assert ppt_geometry(browser_shape) == 'gear9' and str(browser_shape.fill.fore_color.rgb) == 'BF5C21'
        assert str(browser_shape.line.color.rgb) == '1F476B'
        (api.root / 'preset-manifest.json').write_text(json.dumps({
            'catalog_size': len(catalog), 'native_geometry_passed': len(records), 'browser_passed': True,
            'job_id': job['id'], 'deck_sha256': hashlib.sha256(file).hexdigest(), 'presets': records,
            'actual_llm_calls': 0, 'simulated_llm_calls': 0}, indent=2) + '\n', encoding='utf-8')
    print('PPT_PRESET_MANIFEST', api.root / 'preset-manifest.json', flush=True)


def render(root):
    from PIL import Image

    root = root.resolve(strict=True)
    proof = json.loads((root / 'preset-manifest.json').read_text(encoding='utf-8'))
    wsl = ['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec']

    def path(local):
        return subprocess.check_output(wsl + ['wslpath', '-a', str(local)], text=True).strip()

    profile = subprocess.check_output(wsl + ['mktemp', '-d', '/tmp/vf-ppt-render-XXXXXXXX'], text=True).strip()
    assert profile.startswith('/tmp/vf-ppt-render-')
    subprocess.run(wsl + ['soffice', '--headless', '-env:UserInstallation=file://' + profile,
                          '--convert-to', 'pdf:impress_pdf_Export', '--outdir', path(root),
                          path(root / 'ppt-presets.pptx'), path(root / 'browser-shape.pptx')], check=True, timeout=90)
    subprocess.run(wsl + ['pdftoppm', '-r', '144', '-png', path(root / 'ppt-presets.pdf'), path(root / 'slide')],
                   check=True, timeout=60)
    blue = lambda red, green, value: value > 80 and value > green + 25 and value > red + 43
    for page in sorted(root.glob('slide-*.png')):
        number = int(page.stem.rsplit('-', 1)[1])
        with Image.open(page) as raw:
            image = raw.convert('RGB')
            for item in proof['presets']:
                if item['page'] != number:
                    continue
                x, y = item['x'] * image.width, item['y'] * image.height
                right, bottom = x + item['width'] * image.width, y + item['height'] * image.height
                tile = image.crop((round(x - 6), round(y - 6), round(right + 6), round(bottom + 6)))
                item['native_ink_pixels'] = sum(blue(*pixel) for pixel in tile.getdata())
                item['rendered'] = item['native_ink_pixels'] >= 15
    proof['renderer'] = subprocess.check_output(wsl + ['soffice', '--version'], text=True).strip()
    proof['actual_rendered_presets'] = sum(item.get('rendered', False) for item in proof['presets'])
    proof['passed'] = proof['actual_rendered_presets'] == 187 and (root / 'browser-shape.pdf').is_file()
    (root / 'preset-render.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    assert proof['passed'], [item['preset'] for item in proof['presets'] if not item.get('rendered')]


if __name__ == '__main__':
    if sys.argv[1:] == ['--edge']:
        check_browser(json.load(sys.stdin))
    elif len(sys.argv) == 3 and sys.argv[1] == '--render':
        render(Path(sys.argv[2]))
    else:
        exercise_api()
