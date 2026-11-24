"""Actual registered thermal workflow plus engineering templates; no LLM calls."""
import argparse
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import re
import time
from urllib.parse import quote
import uuid
import xml.etree.ElementTree as ET
import zipfile

import httpx

from app.services.casedict import set_block
from registered_cae_benchmarks import run_benchmark
from testsupport import IsolatedServer

ROOT = Path(__file__).resolve().parents[2]
MANUAL = '# User maintained notes\n\nKEEP-USER-DRAFT-EXACT\n'
REFERENCE = ROOT / 'tmp/docker-deployment-20261117/office-layout-f59d586df8/reference.docx'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class EngineeringServer(IsolatedServer):
    def __init__(self, label):
        super().__init__('engineering-templates')
        self.resources = {}
        self.checks = []

    def admitted(self, method, path, body, **options):
        until = time.monotonic() + 30
        while True:
            result = self.call(method, path, body, expect=(200, 409), **options)
            if not isinstance(result, dict) or set(result) != {'detail'}:
                return result
            assert isinstance(result['detail'], dict) and result['detail'].get('code') == 'storage_quota_busy', result
            assert time.monotonic() < until, 'Storage admission stayed busy'
            time.sleep(.2)

    def put(self, path, content):
        return self.admitted('PUT', self.prefix + '/file', {'path': path, 'content': content})

    def upload(self, path, data):
        until = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=60) as client:
            while True:
                response = client.post(self.base + '/api/v1' + self.prefix + '/upload',
                    params={'path': path}, headers={'Authorization': 'Bearer ' + self.token},
                    files={'file': (Path(path).name, data, 'application/octet-stream')})
                detail = response.json().get('detail') if response.status_code == 409 else None
                if not isinstance(detail, dict) or detail.get('code') != 'storage_quota_busy':
                    break
                assert time.monotonic() < until, 'Upload admission stayed busy'
                time.sleep(.2)
        assert response.status_code == 200, (path, response.status_code, response.text[:500])
        self.resources[path] = data

    def create_case(self, name):
        case = super().create_case('Actual engineering template report')
        current = self.call('GET', self.prefix + '/caseDict')
        for invalid in [{'referenceDoc': {'invalid': 'block'}}, {'css': {'invalid': 'block'}}]:
            rejected = self.call('PUT', self.prefix + '/caseDict', {
                'content': set_block(current['content'], 'report', invalid),
                'expected_revision': current['dict_revision']}, expect=422)
            assert 'report' in str(rejected)
            assert self.call('GET', self.prefix + '/caseDict')['content'] == current['content']
        self.check('invalid_template_configuration_rejected_without_dictionary_change')
        content = set_block(current['content'], 'report', {
            'title': '"Engineering template acceptance"',
            'referenceDoc': '"templates/reference.docx"',
            'css': ['"styles/main.css"', '"styles/extra.css"']})
        self.admitted('PUT', self.prefix + '/caseDict',
                      {'content': content, 'expected_revision': current['dict_revision']})
        self.upload('E-report/templates/reference.docx', REFERENCE.read_bytes())
        for path, text in {
            'E-report/styles/main.css': '@import "nested/theme.css";\nbody { max-width: 60em; }',
            'E-report/styles/nested/theme.css': 'h1 { color: #135c72; }',
            'E-report/styles/extra.css': 'table { border-collapse: collapse; }',
        }.items():
            self.put(path, text)
            self.resources[path] = text.encode()
        self.put('E-report/report.md', MANUAL)
        return case

    def raw(self, path):
        return self.call('GET', self.prefix + '/raw?path=' + quote(path, safe=''), raw=True)

    def check(self, label, **details):
        self.checks.append({'name': label, **details})
        (self.root / 'templates.json').write_text(json.dumps({
            'checks': self.checks, 'real_model_calls': 0, 'mock_llm_calls': 0,
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print('ENGINEERING_TEMPLATE_PASS ' + label, flush=True)

    def stopped(self, jid):
        until = time.monotonic() + 240
        while time.monotonic() < until:
            job = self.call('GET', '/jobs/' + str(jid))
            if job['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
                if (job.get('attempt') or {}).get('process_stop', {}).get('verified'):
                    assert job['status'] == 'succeeded', (jid, job.get('log_backlog', '')[-1800:])
                    children = job['attempt']['child_processes']['children'].values()
                    assert all(child['stop']['verified'] for child in children)
                    (self.root / f'template-job-{jid}.json').write_text(json.dumps(job, indent=2) + '\n', 'utf8')
                    return job
            time.sleep(.2)
        raise TimeoutError('Report native scope did not terminate: ' + str(jid))

    def download(self, publication, path):
        return self.call('GET', self.prefix + '/report/artifact?publication_id=' + publication +
                         '&path=' + quote(path, safe=''), raw=True)

    def inspect_report(self, color, style, *, compose):
        result = self.call('GET', self.prefix + '/report/results')
        assert not result['publication']['stale']
        manifest = result['manifest']
        assert manifest['mode'] == ('compose' if compose else 'markdown')
        for path, data in self.resources.items():
            assert manifest['inputs'][path]['sha256'] == sha(data), path
        if compose:
            assert 'E-report/report.md' not in manifest['inputs']
        publication = result['publication']['id']
        outputs = {}
        for path, record in manifest['artifacts'].items():
            data = self.download(publication, path)
            assert sha(data) == record['sha256']
            outputs[path] = data
            (self.root / (publication + '-' + Path(path).name)).write_bytes(data)
            if path.endswith('.html'):
                text = data.decode()
                styles=text
                for _ in range(4):
                    embedded=re.findall(r'data:text/css;charset=utf-8;base64,([A-Za-z0-9+/=]+)',styles)
                    styles+='\n'+'\n'.join(base64.b64decode(value).decode() for value in embedded)
                assert color in styles
                row=re.search(r'<td>mean.T@1</td>\s*<td>volumeAverages</td>\s*<td>[^<]+</td>\s*<td>([^<]+)</td>',text)
                assert row and math.isclose(float(row[1]),350,rel_tol=0,abs_tol=1e-6)
                assert 'KEEP-USER-DRAFT-EXACT' not in text
            if path.endswith('.docx'):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    assert style.encode() in archive.read('word/styles.xml')
                    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                    tree=ET.fromstring(archive.read('word/document.xml'))
                    rows=[[''.join(cell.itertext()) for cell in row.findall('w:tc',ns)] for row in tree.findall('.//w:tr',ns)]
                    metric=next(row for row in rows if row[:2]==['mean.T@1','volumeAverages'])
                    assert math.isclose(float(metric[3]),350,rel_tol=0,abs_tol=1e-6)
        assert any(p.endswith('.docx') for p in outputs) and any(p.endswith('.html') for p in outputs)
        return publication, outputs

    def rebuild_workflow(self):
        flow = self.admitted('POST', self.prefix + '/workflow-runs', {
            'target': 'report', 'run_dir': 'C-run/conduction', 'parameters': {'report_mode': 'compose'},
            'expected_revision': self.call('GET', self.prefix + '/caseDict')['dict_revision'],
            'budget': {'timeout_seconds': 300, 'max_log_bytes': 16777216}},
            extra_headers={'Idempotency-Key': str(uuid.uuid4())})
        until = time.monotonic() + 300
        while time.monotonic() < until:
            flow = self.call('GET', '/workflow-runs/' + flow['id'])
            assert flow['status'] not in {'failed', 'cancelled', 'interrupted'}, flow
            if flow['status'] == 'succeeded':
                for step in flow['steps']:
                    for attempt in step['attempts']:
                        self.stopped(attempt['job_id'])
                return flow
            time.sleep(.2)
        raise TimeoutError('Engineering report rebuild did not complete')


def measure(api, flow, report):
    first, originals = api.inspect_report('#135c72', '8C245B', compose=True)
    assert api.raw('E-report/report.md') == MANUAL.encode()
    api.check('real_geometry_mesh_solve_postprocess_compose_seals_templates_preserves_user_draft',
              publication_id=first, numerical=report['numerical'])
    before_jobs = {j['id'] for j in api.call('GET', api.prefix + '/jobs')}
    before_flows = {f['id'] for f in api.call('GET', api.prefix + '/workflow-runs')}
    api.put('E-report/styles/nested/theme.css', '@import "missing.css";')
    rejected = api.call('POST', api.prefix + '/workflow-runs', {
        'target': 'report', 'run_dir': 'C-run/conduction', 'parameters': {'report_mode': 'compose'},
        'expected_revision': api.call('GET', api.prefix + '/caseDict')['dict_revision']}, expect=(400, 409))
    assert ('native_preparation_failed' in str(rejected) or 'missing.css' in str(rejected)), rejected
    assert {j['id'] for j in api.call('GET', api.prefix + '/jobs')} == before_jobs
    for pending in api.call('GET', api.prefix + '/workflow-runs'):
        if pending['id'] not in before_flows and pending['status'] not in {'failed', 'cancelled', 'succeeded', 'interrupted'}:
            api.call('POST', '/workflow-runs/' + pending['id'] + ':cancel', {'expected_version': pending['version']})
    for path, data in originals.items():
        assert api.download(first, path) == data
    api.check('missing_nested_template_resource_rejected_before_report_job_preserves_originals', response=rejected)
    text = 'h1 { color: #8c245b; }'
    api.put('E-report/styles/nested/theme.css', text)
    api.resources['E-report/styles/nested/theme.css'] = text.encode()
    with zipfile.ZipFile(io.BytesIO(REFERENCE.read_bytes())) as original:
        target = io.BytesIO()
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as updated:
            for name in original.namelist():
                data = original.read(name)
                if name == 'word/styles.xml':
                    data = data.replace(b'8C245B', b'247A49')
                updated.writestr(name, data)
    api.upload('E-report/templates/reference.docx', target.getvalue())
    assert api.call('GET', api.prefix + '/report/results')['publication']['stale']
    api.rebuild_workflow()
    second, _ = api.inspect_report('#8c245b', '247A49', compose=True)
    assert first != second and api.raw('E-report/report.md') == MANUAL.encode()
    for path, data in originals.items():
        assert api.download(first, path) == data
    api.check('changed_template_and_nested_css_new_report_changes_historical_bytes_preserved',
              old_publication=first, new_publication=second)

    # Explicit user-style overwrite uses the original draft hash and preserves a backup.
    created = api.admitted('POST', api.prefix + '/report:compose?wait=false',
        {'overwrite': True, 'expected_sha256': sha(MANUAL.encode())},
        extra_headers={'Idempotency-Key': str(uuid.uuid4())})
    api.stopped(created['job_id'])
    draft = api.raw('E-report/report.md').decode()
    assert draft.startswith('---\nreference-doc: "templates/reference.docx"\n')
    assert 'css: ["styles/main.css", "styles/extra.css"]' in draft and '| mean.T@1 | volumeAverages |' in draft
    composed = api.call('GET', api.prefix + '/report/compose-result?publication_id=' + created['publication_id'])
    assert composed['backup'] and api.raw(composed['backup']) == MANUAL.encode()
    built = api.admitted('POST', api.prefix + '/report:build', {},
        extra_headers={'Idempotency-Key': str(uuid.uuid4())})
    api.stopped(built['job_id'])
    third, _ = api.inspect_report('#8c245b', '247A49', compose=False)
    api.check('generated_editable_draft_carries_templates_backup_and_real_Pandoc_export', publication_id=third)
    report['engineering_template_checks'] = api.checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('review')
    args = parser.parse_args()
    review = json.loads((ROOT / args.review).read_text('utf8'))
    assert review['classification'] == 'no_llm'
    for path, digest in review['sources'].items():
        assert sha((ROOT / path).read_bytes()) == digest, path
    result = run_benchmark('conduction', api_factory=EngineeringServer, measure_completed=measure)
    assert len(result['engineering_template_checks']) == 5 and result['passed']


if __name__ == '__main__':
    main()
