"""Publish on two real PowerPoint layouts and render the resulting editable presentation."""
from io import BytesIO
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode
from zipfile import ZipFile

import httpx
from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.util import Inches

from testsupport import IsolatedServer


TITLE = 'Air-side thermal performance'
BODY = ['Fan sweep complete', 'Flow model compared with measured temperature']
NOTES = 'Open with the measurement method.\nDescribe the uncertainty in the final result.'
THEME = 'ppt/theme/theme1.xml'
DRAWING = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}


def make_template():
    deck = Presentation()
    opening = deck.slides.add_slide(deck.slide_layouts[0])
    opening.shapes.title.text = 'Laboratory template cover'
    opening.placeholders[1].text = 'Original series'
    appendix = deck.slides.add_slide(deck.slide_layouts[1])
    appendix.shapes.title.text = 'Calibration archive'
    chart = CategoryChartData()
    chart.categories = ['A', 'B', 'C']
    chart.add_series('Wall temperature', (31, 45, 53))
    appendix.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(2), Inches(2), Inches(5), Inches(3), chart)
    base = BytesIO()
    deck.save(base)
    modified = BytesIO()
    with ZipFile(base) as original, ZipFile(modified, 'w') as archive:
        for member in original.infolist():
            data = original.read(member)
            if member.filename == THEME:
                theme = etree.fromstring(data)
                theme.xpath('.//a:clrScheme/a:accent1/a:srgbClr', namespaces=DRAWING)[0].set('val', '156555')
                data = etree.tostring(theme, encoding='UTF-8')
            archive.writestr(member, data)
    return modified.getvalue()


def exercise(api):
    api.create_case('PPT template and style acceptance')
    template = make_template()
    source = 'E-report/template-author.pptx'
    output = 'E-report/build/theme-and-styles.pptx'
    original = hashlib.sha256(template).hexdigest()
    reply = httpx.post(api.base + '/api/v1' + api.prefix + '/upload', params={'path': source},
                       headers={'Authorization': 'Bearer ' + api.token},
                       files={'file': ('template-author.pptx', template)}, trust_env=False)
    reply.raise_for_status()
    request = {'output': output, 'template': source, 'title': TITLE, 'slides': [
        {'title': TITLE, 'template_layout': {'master': 0, 'layout': 1}, 'bullets': BODY, 'notes': NOTES},
        {'title': 'Pressure drop summary', 'template_layout': {'master': 0, 'layout': 2},
         'text': 'Water-side pressure loss was below the target.'},
        {'title': 'Styled measured results', 'beautify': True, 'bullets': ['Probe layout verified'],
         'table': {'headers': ['Location', 'Temperature'], 'rows': [['Inlet', '48 C'], ['Wall', '81.2 C']]}},
    ]}
    submitted = api.call('POST', api.prefix + '/ppt:build', request, expect=(200, 202))
    job = api.wait_job(submitted['job_id'])
    assert job['attempt']['process_stop']['verified']
    document = api.call('GET', api.prefix + '/raw?' + urlencode({'path': output}), raw=True)
    assert hashlib.sha256(api.call('GET', api.prefix + '/raw?' + urlencode({'path': source}), raw=True)).hexdigest() == original
    deck = Presentation(BytesIO(document))
    assert len(deck.slides) == 5
    assert deck.slides[0].shapes.title.text == 'Laboratory template cover'
    chart = next(shape.chart for shape in deck.slides[1].shapes if shape.has_chart)
    assert list(chart.series[0].values) == [31, 45, 53]
    for index, title in ((2, TITLE), (3, 'Pressure drop summary')):
        slide = deck.slides[index]
        selected = request['slides'][index - 2]['template_layout']['layout']
        assert slide.slide_layout.name == deck.slide_layouts[selected].name
        assert slide.shapes.title.text == title and slide.shapes.title.is_placeholder
        body = next(shape for shape in slide.placeholders if shape.name == 'VibeFlowBody')
        assert body.placeholder_format.type in {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT}
    assert deck.slides[2].notes_slide.notes_text_frame.text == NOTES
    styled = deck.slides[4]
    assert str(styled.background.fill.fore_color.rgb) == 'F5F8FC'
    table = next(shape.table for shape in styled.shapes if shape.has_table)
    assert table.cell(2, 1).text == '81.2 C' and str(table.cell(0, 0).fill.fore_color.rgb) == '154A71'
    with ZipFile(BytesIO(template)) as original_zip, ZipFile(BytesIO(document)) as output_zip:
        assert original_zip.read(THEME) == output_zip.read(THEME)
    (api.root / 'template-original.pptx').write_bytes(template)
    (api.root / 'published-deck.pptx').write_bytes(document)
    return {'verified_worker_stop': True, 'source_sha256': original, 'published_sha256': hashlib.sha256(document).hexdigest(),
            'job_id': submitted['job_id'], 'original_native_chart': [31, 45, 53], 'inherited_native_layouts': 2,
            'editable_speaker_notes': True, 'editable_beautified_table': True, 'actual_llm_calls': 0, 'simulated_llm_calls': 0}


def render(root):
    wsl = ['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec']
    location = subprocess.check_output(wsl + ['wslpath', '-a', str(root)], text=True).strip()
    profile = subprocess.check_output(wsl + ['mktemp', '-d', '/tmp/vf-ppt-template-XXXXXXXX'], text=True).strip()
    subprocess.run(wsl + ['soffice', '--headless', '-env:UserInstallation=file://' + profile,
                          '--convert-to', 'pdf:impress_pdf_Export', '--outdir', location,
                          location + '/published-deck.pptx'], timeout=90, check=True)
    subprocess.run(wsl + ['pdftoppm', '-r', '120', '-png', location + '/published-deck.pdf', location + '/layout'],
                   timeout=60, check=True)
    pages = sorted(root.glob('layout-*.png'))
    assert len(pages) == 5
    readback = subprocess.check_output(wsl + ['pdftotext', location + '/published-deck.pdf', '-']).decode('utf-8')
    normalized = ' '.join(readback.split()).casefold()
    for text in ('Laboratory template cover', 'Calibration archive', TITLE, 'Pressure drop summary', '81.2 C'):
        assert text.casefold() in normalized, text
    assert '• fan sweep complete' in normalized and '• •' not in normalized
    return {'rendered_pages': len(pages), 'original_and_generated_text_read_back': True}


def main():
    api = IsolatedServer('ff-e21-ppt-template')
    with api:
        proof = exercise(api)
    proof.update(render(api.root), passed=True)
    (api.root / 'ppt-template.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    print('PPT_TEMPLATE', api.root / 'ppt-template.json', flush=True)


if __name__ == '__main__':
    main()
