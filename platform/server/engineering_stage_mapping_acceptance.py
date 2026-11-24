"""Run actual CAD/mesh/solve/post/report stage gates and compare both status mirrors."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    expect.set_options(timeout=25000)
    async with async_playwright() as driver:
        chromium = await driver.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1500, 'height': 950})
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base'])
            + ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        try:
            await page.goto(settings['base'])
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
            heading = page.get_by_role('heading', name='当前阶段 E 报告', exact=True)
            if not await heading.is_visible():
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if await tab.is_visible():
                    await tab.click()
            await expect(heading).to_be_visible()
            await expect(page.get_by_text('readyForReport', exact=True)).to_be_visible()
            await page.screenshot(path=str(Path(settings['root']) / 'stage-mapping-E.png'))
        finally:
            await chromium.close()


def verify(args):
    from app.services.casedict import CASE_DICT_PATH, parse_casedict, set_block
    from case_workflow_thermal_fixture import RUN, GEOMETRY, case_dictionary, thermal_files
    from testsupport import IsolatedServer

    api = IsolatedServer('stage-mapping', app='stage_mapping_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    (api.root / 'stage_mapping_fixture.py').write_text('from app.main import app\n'
        + 'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temporary = api.root / 'browser-tmp'
    temporary.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0,
              'native_jobs': {}, 'status_mapping': [], 'missing_evidence': {}}

    def snapshot():
        return api.call('GET', api.prefix + '/caseDict')

    def mapping(stage, status):
        rendered = snapshot()
        mirror = api.call('GET', api.prefix)
        raw = (case_root / CASE_DICT_PATH).read_bytes()
        assert raw == rendered['content'].encode('utf8')
        assert hashlib.sha256(raw).hexdigest() == rendered['content_sha256']
        assert rendered['workflow_state'] == mirror['workflow_state'] == stage
        assert rendered['dict_revision'] == mirror['dict_revision']
        assert rendered['parsed']['caseMeta'] == json.loads(mirror['case_meta'])
        assert rendered['parsed']['caseMeta']['status'] == status
        chain = api.call('GET', api.prefix + '/provenance')
        assert chain['chain_valid'] and chain['events'][0]['casedict_sha256'] == rendered['content_sha256']
        report['status_mapping'].append({'stage': stage, 'status': status,
            'revision': rendered['dict_revision'], 'dictionary_sha256': rendered['content_sha256']})
        return rendered

    def advance(stage, **fields):
        before = snapshot()
        expect = fields.pop('expect', 200)
        body = {'state': stage, 'confirmed': True, 'expected_revision': before['dict_revision']} | fields
        result = api.call('POST', api.prefix + '/caseDict:advance', body, expect=expect)
        if expect != 200:
            assert snapshot() == before
        return result

    def inspect_run(identifier, waiting_stage=None):
        timeout = time.monotonic() + 240
        while time.monotonic() < timeout:
            flow = api.call('GET', '/workflow-runs/' + identifier)
            if flow['status'] in {'failed', 'cancelled', 'interrupted'}:
                raise AssertionError(str(flow)[:3000])
            if waiting_stage:
                step = next(step for step in flow['steps'] if step['key'] == 'confirm_' + waiting_stage)
                if step['status'] == 'awaiting_confirmation':
                    return flow, step
            elif flow['status'] == 'succeeded':
                return flow
            time.sleep(.15)
        raise TimeoutError('Workflow did not reach ' + str(waiting_stage or 'succeeded'))

    def actual_job(identifier):
        result = api.wait_job(identifier, timeout=220)
        assert result['attempt']['process_stop']['verified'] and result['attempt']['phase'] == 'succeeded'
        return result

    def run_stage(target, stage, status):
        before = snapshot()
        state = api.call('POST', api.prefix + '/workflow-runs', {'target': target, 'run_dir': RUN,
            'expected_revision': before['dict_revision'], 'parameters': {'geometry_mode': 'export', 'report_mode': 'compose'},
            'budget': {'timeout_seconds': 180, 'max_log_bytes': 4*1024*1024}}, extra_headers={'Idempotency-Key': str(uuid.uuid4())})
        run, step = inspect_run(state['id'], stage)
        for producer in run['steps']:
            if producer['status'] == 'succeeded':
                for attempt in producer['attempts']:
                    actual_job(attempt['job_id'])
        assert snapshot() == before
        fields = {'workflow_step_id': step['id'], 'expected_step_version': step['version']}
        advance(stage, **fields, confirmed=False, expect=409)
        receipt = advance(stage, **fields)
        assert receipt['dict_revision'] == before['dict_revision'] + 1 and receipt['workflow_state'] == stage
        mapping(stage, status)
        final = inspect_run(run['id'])
        for step in final['steps']:
            for attempt in step['attempts']:
                job = actual_job(attempt['job_id'])
                publication = job['attempt']['outputs']['publication']
                assert publication['status'] == 'committed'
                report['native_jobs'][step['key']] = {'job_id': job['id'], 'publication_id': publication['id'], 'verified_stop': True}
        print(f'STAGE_MAPPING_{stage}_CONFIRMED', flush=True)

    try:
        with api:
            case = api.create_case('Actual engineering stage mapping')
            identity = api.call('GET', api.prefix)
            case_root = (api.root / 'files' / identity['storage_prefix']).resolve()
            assert case_root.is_relative_to(api.root.resolve())
            initial = snapshot()
            text = case_dictionary(initial['content'])
            metadata = parse_casedict(text)['caseMeta'] | {'status': 'draft'}
            text = set_block(text, 'caseMeta', metadata)
            api.call('PUT', api.prefix + '/caseDict', {'content': text, 'expected_revision': initial['dict_revision']})
            prepared = mapping('A', 'draft')
            bad_status = set_block(prepared['content'], 'caseMeta', metadata | {'status': 'readyForMesh'})
            rejected = api.call('PUT', api.prefix + '/caseDict', {'content': bad_status,
                'expected_revision': prepared['dict_revision']}, expect=422)
            assert 'caseMeta.status' in rejected['detail'] and snapshot() == prepared
            advance('C', expect=409)
            advance('B', expected_revision=0, expect=409)

            for target, stage, status in (('geometry', 'B', 'readyForMesh'), ('mesh', 'C', 'readyForRun'),
                                         ('solve', 'D', 'readyForPost'), ('report', 'E', 'readyForReport')):
                rejected = advance(stage, expect=409)
                report['missing_evidence'][stage] = rejected['detail']
                if stage == 'B':
                    for path, value in thermal_files().items():
                        api.put(path, value)
                    prepared_geometry = api.put('A-geo/geometry.py', GEOMETRY)
                    if prepared_geometry.get('auto_preview_job'):
                        actual_job(prepared_geometry['auto_preview_job'])
                if stage == 'E':
                    assert '成功后处理' in rejected['detail']
                    pointer = case_root / 'D-post/data/latest-postprocess.json'
                    assert not pointer.exists()
                    pointer.parent.mkdir(parents=True, exist_ok=True)
                    fault_receipts = {}
                    try:
                        for name, payload in (
                            ('array', b'[]'), ('null', b'null'), ('torn', b'{"job_id":'),
                            ('object_id', b'{"job_id":{"value":1}}'), ('bool_id', b'{"job_id":true}'),
                            ('fractional_id', b'{"job_id":1.25}'), ('oversized_id', b'{"job_id":999999999999999999999}'),
                            ('wrong_job_type', json.dumps({'job_id': report['native_jobs']['solve']['job_id']}).encode('utf8')),
                        ):
                            pointer.write_bytes(payload)
                            reply = advance('E', expect=409)
                            assert '成功后处理' in reply['detail'] and pointer.read_bytes() == payload
                            fault_receipts[name] = reply['detail']
                    finally:
                        pointer.unlink(missing_ok=True)
                    report['legacy_e_unusable_pointer_denials'] = fault_receipts
                run_stage(target, stage, status)

            metrics = api.call('GET', api.prefix + '/postprocess/results')['metrics']
            assert metrics['readiness']['report_ready']
            report['report_ready'] = True
            published = api.call('GET', api.prefix + '/report/results')['publication']
            assert not published['stale'] and published['id'] == report['native_jobs']['report']['publication_id']
            settings = {'root': str(api.root), 'base': api.base, 'token': api.token, 'case': case['name'], 'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, timeout=110, check=True,
                env=dict(os.environ, TEMP=str(temporary), TMP=str(temporary)))
            report['browser_stage_e_and_mapped_status'] = True

            report_snapshot = snapshot()
            completed = set_block(report_snapshot['content'], 'caseMeta', report_snapshot['parsed']['caseMeta'] | {'status': 'completed'})
            api.call('PUT', api.prefix + '/caseDict', {'content': completed, 'expected_revision': report_snapshot['dict_revision']})
            mapping('E', 'completed')
            advance('A')
            mapping('A', 'readyForGeometry')
        report['passed'] = api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'stage-mapping-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('ENGINEERING_STAGE_MAPPING_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
