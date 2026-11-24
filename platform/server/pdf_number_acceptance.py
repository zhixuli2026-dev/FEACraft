"""E13: real PDF upload, registered numbering, rendered footers and immutable readback."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from struct import unpack
import subprocess
import sys
import time
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import httpx
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject, RectangleObject


SOURCE = 'Ref/engineering.pdf'
OUTPUT = 'Ref/engineering.numbered.pdf'
BROKEN = b'%PDF-1.7\nThis uploaded PDF is truncated.\n'
PAGES = (
    ((10, -20, 350, 432), (30, 20, 328, 393), 0),
    ((-40, 25, 590, 393), (-16, 55, 520, 373), 90),
    ((-80, -60, 300, 406), (-105, -28, 271, 376), 180),
    ((30, 40, 378, 574), (48, 61, 342, 550), 270),
)


def digest(data):
    return sha256(data).hexdigest()


def sample(version):
    writer = PdfWriter()
    writer.add_metadata({'/Title': f'E13 engineering sample {version}', '/Author': 'VibeFlow acceptance',
                         '/Subject': 'Independent PDF page numbering', '/E13Fixture': 'Keep this field'})
    # The existing page text intentionally uses the old hard-coded footer resource name.
    original_font = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Courier'), NameObject('/Encoding'): NameObject('/WinAnsiEncoding'),
    }))
    for index, (media, crop, rotation) in enumerate(PAGES, 1):
        page = writer.add_blank_page(width=media[2]-media[0], height=media[3]-media[1])
        page[NameObject('/MediaBox')] = RectangleObject(media)
        page[NameObject('/CropBox')] = RectangleObject(crop)
        page[NameObject('/Rotate')] = NumberObject(rotation)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/VFPageNumberFont'): original_font,
        })})
        x, y = max(media[0], crop[0]) + 30, max(media[1], crop[1]) + 83
        text = DecodedStreamObject()
        # Legitimate carried CTM and clip apply to the sample, but not the independent new footer.
        text.set_data((f'1 0 0 1 11 17 cm\n{x-10} {y-16} 207 54 re W n\n'
                       f'BT /VFPageNumberFont 12 Tf 1 0 0 1 {x} {y} Tm (E13-V{version}-PAGE-{index}) Tj ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(text)
    writer.add_outline_item('Original first page', 0)
    writer.add_outline_item('Original rotated last page', 3)
    writer.set_page_label(0, 3, style='/D', prefix='Original-', start=9)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def pdf_document(data):
    return PdfReader(BytesIO(data))


def verify_pdf(source, numbered, version, output):
    old, new = pdf_document(source), pdf_document(numbered)
    assert dict(old.metadata) == dict(new.metadata)
    assert old.page_labels == new.page_labels == ['Original-9', 'Original-10', 'Original-11', 'Original-12']
    assert [(item.title, old.get_destination_page_number(item)) for item in old.outline] == [
        (item.title, new.get_destination_page_number(item)) for item in new.outline]
    assert len(old.pages) == len(new.pages) == len(PAGES)
    for index, (before, after) in enumerate(zip(old.pages, new.pages, strict=True), 1):
        assert tuple(before.mediabox) == tuple(after.mediabox) and tuple(before.cropbox) == tuple(after.cropbox)
        assert before.rotation == after.rotation
        assert before['/Contents'].get_data() == after['/Contents'][1].get_object().get_data()
        assert after['/Resources']['/Font']['/VFPageNumberFont']['/BaseFont'] == '/Courier'
        assert after['/Resources']['/Font']['/VFPageNumberFont2']['/BaseFont'] == '/Helvetica'
        foot = after['/Contents'][-1].get_object().get_data()
        assert b'/VFPageNumberFont2 9 Tf\n0.35 g\n' in foot
        assert f'({index} / {len(PAGES)}) Tj'.encode('ascii') in foot
    output.write_bytes(numbered)

    # Poppler sees the rotated, clipped display viewport independently of pypdf and the worker.
    assert output.drive.upper() == 'D:'
    wsl_path = '/mnt/d' + output.as_posix()[2:]
    command = ['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec']
    rendered = subprocess.run([*command, 'pdftotext', '-cropbox', '-bbox-layout', wsl_path, '-'],
                              capture_output=True, check=True, timeout=35).stdout
    output.with_suffix('.bbox.html').write_bytes(rendered)
    raster = output.with_suffix('')
    subprocess.run([*command, 'pdftoppm', '-cropbox', '-r', '72', '-png', wsl_path, '/mnt/d'+raster.as_posix()[2:]],
                   capture_output=True, check=True, timeout=35)
    view = ET.fromstring(rendered)
    pages = view.findall('.//{*}page')
    assert len(pages) == len(PAGES)
    receipts = []
    for index, page in enumerate(pages, 1):
        # Poppler's XHTML reports unrotated page dimensions even when text bounds have rotated.
        image = raster.with_name(f'{raster.name}-{index}.png')
        with image.open('rb') as stream:
            prefix = stream.read(24)
        assert prefix.startswith(b'\x89PNG\r\n\x1a\n') and prefix[12:16] == b'IHDR'
        width, height = unpack('>II', prefix[16:24])
        expected = f'{index} / {len(PAGES)}'
        lines = page.findall('.//{*}line')
        assert any(f'E13-V{version}-PAGE-{index}' in ''.join(line.itertext()) for line in lines)
        footer = [line for line in lines if ' '.join(word.text or '' for word in line.findall('{*}word')) == expected]
        assert len(footer) == 1, (index, rendered.decode('utf8')[:240])
        left, bottom = float(footer[0].attrib['xMin']), float(footer[0].attrib['yMax'])
        right, top = float(footer[0].attrib['xMax']), float(footer[0].attrib['yMin'])
        assert abs((left+right)/2-width/2) < 1.0 and 17 < height-bottom < 25 and 23 < height-top < 32, (
            index, width, height, left, right, top, bottom)
        receipts.append({'page': index, 'rotation': PAGES[index-1][2], 'label': expected,
                         'rendered_width': width, 'rendered_height': height,
                         'footer_center_x': round((left+right)/2, 3), 'distance_to_bottom': round(height-bottom, 3)})
    return receipts


def main():
    from testsupport import IsolatedServer
    from app.services.process_identity_native import probe

    api = IsolatedServer('e13-number-existing-pdf')
    print('E13_PDF_EVIDENCE', api.root, flush=True)
    result = {'passed': False, 'model_calls': 0, 'storage': 'fs', 'database': 'sqlite', 'jobs': [], 'retries': 0}

    def request(method, suffix, expected=200, *, wait_busy=False, headers=None, **kwargs):
        deadline = time.monotonic()+25
        with httpx.Client(timeout=75, trust_env=False) as client:
            while True:
                response = client.request(method, api.base+'/api/v1'+api.prefix+suffix,
                    headers={'Authorization':'Bearer '+api.token, **(headers or {})}, **kwargs)
                if wait_busy and response.status_code == 409:
                    detail = response.json().get('detail')
                    if isinstance(detail, dict) and detail.get('code') == 'storage_quota_busy':
                        assert time.monotonic() < deadline, response.text
                        result['retries'] += 1
                        time.sleep(.3)
                        continue
                assert response.status_code == expected, (method, suffix, response.status_code, response.text[:300])
                return response

    def upload(data, replaced=None):
        params = {'path': SOURCE, 'expected_absent': 'true'} if replaced is None else {
            'path': SOURCE, 'expected_sha256': digest(replaced)}
        request('POST', '/upload', params=params, files={'file': ('engineering.pdf', data, 'application/pdf')}, wait_busy=True)

    def submit(key, expected=200, **changes):
        return request('POST', '/pdf:number', expected, json={'input': SOURCE, **changes}, wait_busy=True,
                       headers={'Idempotency-Key': key}).json()

    def raw(path, status=200):
        return request('GET', '/raw?'+urlencode({'path':path}), status).content

    def read_publication(identifier, verb='GET', expected=200, **headers):
        return request(verb, '/report/derivative-artifact?'+urlencode({'publication_id':identifier,'path':OUTPUT}),
                       expected, headers=headers)

    def verify_stop(created, status):
        current = api.wait_job(created['job_id'], timeout=200, expected=status)
        attempt = current['attempt']
        stop = attempt['process_stop']
        assert stop['verified'] and stop['scope_empty'] and not current['completion_pending']
        identity = attempt['process_identity']['host']
        result['jobs'].append({'id': current['id'], 'status': status, 'exit_code':current['exit_code'], 'process': identity})
        return current

    try:
        with api:
            api.create_case('E13 existing PDF numbering')
            workspace = api.call('GET', api.prefix)['workspace_id']
            upload(BROKEN)
            failed = submit('e13-truncated-pdf')
            assert failed['output'] == OUTPUT
            broken_state = verify_stop(failed, 'failed')
            assert broken_state['exit_code'] != 0
            assert submit('e13-truncated-pdf')['job_id'] == failed['job_id']
            assert raw(SOURCE) == BROKEN
            raw(OUTPUT, 404)
            result['failed_worker_preserves_source_and_no_output'] = True

            first, second = sample(1), sample(2)
            upload(first, BROKEN)
            ordinary = submit('e13-corrected-version-one')
            winner = verify_stop(ordinary, 'succeeded')
            old_id = ordinary.get('publication_id') or winner['attempt']['outputs']['publication']['id']
            assert submit('e13-corrected-version-one')['job_id'] == ordinary['job_id']
            assert submit('e13-competing-new-key', 409)['detail']['code'] == 'output_exists'
            assert submit('e13-corrected-version-one', 409, output='Ref/other.pdf')['detail']['code'] == 'idempotency_conflict'
            submit('e13-inplace-refused', 400, output=SOURCE)
            first_out = read_publication(old_id).content
            assert raw(SOURCE) == first and raw(OUTPUT) == first_out
            result['first_rendered_footers'] = verify_pdf(first, first_out, 1, api.root/'numbered-generation-1.pdf')

            request('DELETE','/file?'+urlencode({'path':OUTPUT,'expected_sha256':digest(first_out)}), wait_busy=True)
            upload(second, first)
            fresh = submit('e13-version-two')
            status = verify_stop(fresh, 'succeeded')
            new_id = fresh.get('publication_id') or status['attempt']['outputs']['publication']['id']
            assert new_id != old_id
            old = api.call('GET', api.prefix+'/report/derivative?publication_id='+old_id)
            new = api.call('GET', api.prefix+'/report/derivative?publication_id='+new_id)
            history = api.call('GET', api.prefix+'/report/derivatives?family=pdf-number&limit=1')
            earlier = api.call('GET', api.prefix+'/report/derivatives?family=pdf-number&limit=1&before_id='+new_id)
            assert history[0]['id'] == new_id and earlier[0]['id'] == old_id
            assert old['publication']['stale'] and not old['publication']['current'] and new['publication']['current']
            assert old['files'][0]['sha256'] == digest(first_out)
            assert read_publication(old_id).content == first_out
            second_out = read_publication(new_id).content
            assert new['files'][0]['sha256'] == digest(second_out) and raw(OUTPUT) == second_out and raw(SOURCE) == second
            head = read_publication(old_id, 'HEAD')
            assert not head.content and int(head.headers['content-length']) == len(first_out)
            version = head.headers['x-vibeflow-file-version']
            assert read_publication(old_id, expected=206, Range='bytes=8-72', **{'X-Vibeflow-File-Version': version}).content == first_out[8:73]
            read_publication(old_id, expected=409, **{'X-Vibeflow-File-Version': '0'*64})
            result['second_rendered_footers'] = verify_pdf(second, second_out, 2, api.root/'numbered-generation-2.pdf')
            result['publications'] = [{'id':old_id,'sha256':digest(first_out)}, {'id':new_id,'sha256':digest(second_out)}]

            email = 'e13-pdf-reader@example.com'
            guest = api.call('POST','/auth/register',{'email':email,'name':'Page reader','password':'isolated-e13-reader-123'})['token']
            auth = {'Authorization':'Bearer '+guest}
            read_publication(old_id, expected=404, **auth)
            request('POST','/pdf:number',404,json={'input':SOURCE},headers=auth)
            org = api.call('GET','/org/mine')['org']['id']
            api.call('POST',f'/org/{org}/members',{'email':email,'role':'viewer'},expect=201)
            api.call('POST',f'/org/{org}/invitation:accept',{},extra_headers=auth)
            api.call('PUT',f'/workspaces/{workspace}/sharing',{'org_id':org,'role':'viewer'})
            assert read_publication(old_id, **auth).content == first_out
            request('POST','/pdf:number',404,json={'input':SOURCE},headers=auth)
            result['acl'] = True
        assert len(result['jobs']) == 3
        for job in result['jobs']:
            assert probe(job['process'])['state'] == 'exited'
            job['independently_exited'] = True
        result['passed'] = api.proc.poll() is not None
    finally:
        result['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root/'pdf-number-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf8')
    assert result['passed']
    print('E13_PDF_PASS', api.root/'pdf-number-result.json', flush=True)


if __name__ == '__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    main()
