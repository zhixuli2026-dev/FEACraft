"""Run local browser OCR, edit one region, publish native objects, and render them."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def inputs(root):
    from PIL import Image, ImageDraw, ImageFont

    fonts = Path(os.environ['WINDIR']) / 'Fonts'
    for language, text, filename in [('eng', 'THERMAL CHECK', 'arialbd.ttf'), ('chi_sim', '温度校核', 'msyh.ttc')]:
        image = Image.new('RGB', (960, 540), '#F5F8FC')
        draw = ImageDraw.Draw(image)
        draw.text((64, 55), text, fill='#152941', font=ImageFont.truetype(str(fonts / filename), 66))
        draw.rectangle((65, 220, 425, 396), fill='#176BA2')
        draw.ellipse((585, 205, 827, 447), fill='#B75C30')
        image.save(root / (language + '.png'))


def browser(config):
    from playwright.sync_api import expect, sync_playwright

    root = Path(config['root'])
    checks = {'ocr_loads': [], 'external_requests': [], 'actual_ocr_runs': 0}
    with sync_playwright() as play:
        browser = play.chromium.launch(channel='msedge')
        context = browser.new_context(viewport={'width': 1470, 'height': 920}, locale='zh-CN', accept_downloads=True)
        context.add_init_script('localStorage.setItem("vf_token",' + json.dumps(config['token']) + ');')

        def local_only(route):
            url = route.request.url
            if url.startswith((config['base'] + '/', 'blob:', 'data:')):
                route.continue_()
            else:
                checks['external_requests'].append(url)
                route.abort()

        context.route('**/*', local_only)
        page = context.new_page()
        page.on('response', lambda r: checks['ocr_loads'].append([r.url.split('/')[-1], r.status])
                if re.search('traineddata|tesseract|worker.min', r.url) else None)
        try:
            page.goto(config['base'], wait_until='domcontentloaded')
            page.locator('[data-case-name="Slide reconstruction"]').click(timeout=25000)
            page.locator('.dv-tab:visible').filter(has_text=re.compile('^caseDict$')).click()
            editor = page.get_by_test_id('ppt-editor')
            editor.get_by_text('从图片重建可编辑 PPT', exact=True).click()
            upload = editor.get_by_label('PPT 重建本地图片')
            language = editor.get_by_label('识别语言')
            status = editor.get_by_test_id('ppt-rebuild-status')
            for lang, expected, correction in [('eng', 'THERMAL CHECK', 'THERMAL REVIEW'), ('chi_sim', '温度校核', '温度复核')]:
                language.select_option(lang)
                upload.set_input_files(root / (lang + '.png'))
                editor.get_by_role('button', name='识别文字和图形').click()
                expect(status).to_contain_text('个图形', timeout=90000)
                row = editor.get_by_role('textbox', name='重建文字 1', exact=True)
                text = row.input_value()
                assert ''.join(text.split()) == ''.join(expected.split()), (lang, text)
                checks['actual_ocr_runs'] += 1
                row.fill(correction)
                editor.get_by_test_id('ppt-output').fill('E-report/build/rebuilt-' + lang + '.pptx')
                editor.get_by_test_id('ppt-build').click()
                expect(editor.get_by_test_id('ppt-status')).to_contain_text('PPTX 已生成', timeout=90000)
                with page.expect_download() as completion:
                    editor.get_by_test_id('ppt-download').click()
                completion.value.save_as(root / ('rebuilt-' + lang + '.pptx'))
            assert not checks['external_requests'], checks
            editor.screenshot(path=root / 'reconstruction-controls.png')
        finally:
            (root / 'reconstruction-browser.json').write_text(json.dumps(checks, indent=2) + '\n', encoding='utf-8')
            browser.close()


def main():
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from app.services.ppt_builder import ppt_geometry
    from testsupport import IsolatedServer

    dist = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    api = IsolatedServer('ff-e21-ppt-reconstruction', app='ppt_fixture:app')
    inputs(api.root)
    (api.root / 'ppt_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf-8')
    with api:
        api.create_case('Slide reconstruction')
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], '-X', 'utf8', __file__, '--edge'], check=True,
                       input=json.dumps({'base': api.base, 'token': api.token, 'root': str(api.root)}), text=True, timeout=240)
    wsl = ['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec']
    root = subprocess.check_output(wsl + ['wslpath', '-a', str(api.root)], text=True).strip()
    profile = subprocess.check_output(wsl + ['mktemp', '-d', '/tmp/vf-ppt-rebuild-XXXXXXXX'], text=True).strip()
    reports = []
    for lang, corrected in [('eng', 'THERMAL REVIEW'), ('chi_sim', '温度复核')]:
        name = 'rebuilt-' + lang
        deck = Presentation(str(api.root / (name + '.pptx')))
        assert abs(deck.slide_width / deck.slide_height - 16 / 9) < 0.0001
        shapes = deck.slides[0].shapes
        assert not any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in shapes)
        assert {ppt_geometry(shape) for shape in shapes if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE} == {'rect', 'ellipse'}
        assert corrected in [shape.text for shape in shapes if shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX]
        subprocess.run(wsl + ['soffice', '--headless', '-env:UserInstallation=file://' + profile, '--convert-to',
                              'pdf:impress_pdf_Export', '--outdir', root, f'{root}/{name}.pptx'], check=True, timeout=60)
        subprocess.run(wsl + ['pdftoppm', '-singlefile', '-r', '120', '-png', f'{root}/{name}.pdf', f'{root}/{name}'],
                       check=True, timeout=30)
        result = subprocess.check_output(wsl + ['pdftotext', f'{root}/{name}.pdf', '-']).decode('utf-8')
        assert ''.join(corrected.split()) in ''.join(result.split()), result
        reports.append({'language': lang, 'text': corrected, 'native_shapes': ['rect', 'ellipse'], 'actual_rendered': True})
    (api.root / 'ppt-reconstruction.json').write_text(json.dumps({'passed': True, 'examples': reports,
        'actual_local_ocr_runs': 2, 'actual_llm_calls': 0, 'simulated_llm_calls': 0}, indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8')
    print('PPT_RECONSTRUCTION', api.root / 'ppt-reconstruction.json', flush=True)


if __name__ == '__main__':
    browser(json.load(sys.stdin)) if sys.argv[1:] == ['--edge'] else main()
