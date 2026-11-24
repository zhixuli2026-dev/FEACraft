"""Read native OpenFOAM logs, residuals, postprocessing and registered reports in Edge."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote
import uuid
import zipfile


async def browser(settings):
    from PIL import Image
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    result = {}
    expect.set_options(timeout=25000)
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1500, 'height': 950}, accept_downloads=True)
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base']) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)

        async def open_file(path):
            segments = path.split('/')
            for end in range(1, len(segments)):
                parent = page.locator('[data-path=' + json.dumps('/'.join(segments[:end])) + ']')
                await expect(parent).to_be_visible()
                if await parent.get_attribute('aria-expanded') == 'false':
                    await parent.click()
            await page.locator('[data-path=' + json.dumps(path) + ']').click()

        async def download(button, name):
            async with page.expect_download() as downloading:
                await button.click()
            item = await downloading.value
            assert item.suggested_filename == name, item.suggested_filename
            path = root / name
            await item.save_as(path)
            return path

        try:
            await page.goto(settings['base'])
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
            jobs = page.get_by_role('button', name='作业与日志', exact=True)
            if not await jobs.is_visible():
                await page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^作业$')).click()
            await jobs.click()
            item = page.get_by_role('button', name=re.compile(r'^#' + str(settings['solver_job']) + r'(?:\s|$)'))
            await item.click()
            await expect(item).to_have_attribute('aria-pressed', 'true')
            log = page.get_by_test_id('job-log-view')
            await expect(log).to_contain_text('Initial residual')
            await expect(log).to_contain_text('Final residual')
            await expect(log).to_contain_text('ExecutionTime')
            result['actual_browser_log'] = True

            await page.get_by_role('button', name='残差曲线', exact=True).click()
            chart = page.get_by_role('img', name='求解残差曲线')
            summary = page.get_by_test_id('residual-summary')
            await expect(summary).to_contain_text('末时刻 t=1')
            await expect(summary).to_contain_text('正常结束')
            for series in ('initial', 'final'):
                group = chart.locator(f'[data-field="T"][data-series="{series}"]')
                await expect(group).to_have_count(1)
            actual_tip = chart.locator('[data-field="T"][data-series="final"] circle').first
            log_coordinate = await actual_tip.get_attribute('cy')
            log_y = float(log_coordinate)
            linear = page.get_by_role('button', name='线性轴', exact=True)
            await linear.click()
            await expect(linear).to_have_attribute('aria-pressed', 'true')
            await expect(actual_tip).not_to_have_attribute('cy', log_coordinate)
            linear_y = float(await actual_tip.get_attribute('cy'))
            assert math.isfinite(log_y) and math.isfinite(linear_y) and log_y != linear_y, (log_y, linear_y)
            await chart.screenshot(path=str(root / 'solver-chart.png'))
            exported = await download(page.get_by_role('button', name='导出 PNG', exact=True), 'residual_t1.png')
            with Image.open(exported) as png:
                assert png.format == 'PNG' and png.size == (1280, 520)
            result['residual_final_y'] = {'log': log_y, 'linear': linear_y}

            path = settings['post_dir'] + '/temperature.png'
            await open_file(path)
            picture = page.get_by_test_id('image-img')
            await expect(picture).to_be_visible()
            await page.wait_for_function("document.querySelector('[data-testid=image-img]')?.naturalWidth > 0")
            await picture.screenshot(path=str(root / 'published-temperature-view.png'))
            await open_file(settings['post_dir'] + '/profile.csv')
            table = page.get_by_test_id('csv-viewer')
            await expect(table).to_be_visible()
            await expect(table.get_by_test_id('csv-header')).to_have_count(settings['csv_columns'])
            await expect(table.locator('tbody tr')).to_have_count(settings['csv_rows'])
            result['native_postprocess_project_view'] = {'png': path, 'csv_rows': settings['csv_rows']}

            for suffix in ('html', 'docx'):
                name = 'report.' + suffix
                await open_file('E-report/build/' + name)
                reader = page.get_by_test_id('document-reader')
                await expect(reader).to_have_attribute('data-kind', suffix)
                metric_row = reader.get_by_test_id('document-content').get_by_role('row').filter(has_text='volumeAverages')
                await expect(metric_row).to_contain_text('mean.T@1')
                temperature = float(await metric_row.get_by_role('cell').nth(3).inner_text())
                assert math.isclose(temperature, 350, abs_tol=1e-6), temperature
                await reader.get_by_test_id('report-history-toggle').click()
                publication = reader.locator('[data-publication-id=' + json.dumps(settings['report_id']) + ']')
                await publication.click()
                await expect(publication).to_have_attribute('aria-pressed', 'true')
                plot_link = reader.get_by_test_id('document-content').get_by_role('link', name='temperature.png', exact=True)
                await expect(plot_link).to_have_attribute('href', '/' + path)
                saved = await download(reader.get_by_test_id('document-download'), name)
                actual_hash = hashlib.sha256(saved.read_bytes()).hexdigest()
                assert actual_hash == settings['report_hashes'][suffix]
                result[suffix] = {'published_id': settings['report_id'], 'sha256': actual_hash}
                if suffix == 'html':
                    await plot_link.click()
                    await expect(page.get_by_test_id('image-viewer')).to_have_attribute('data-file', path)
                    await expect(page.get_by_test_id('image-img')).to_be_visible()
                    result['report_plot_link_opens_actual_file'] = True
            await page.get_by_test_id('document-reader').screenshot(path=str(root / 'published-docx-view.png'))
        except Exception:
            await page.screenshot(path=str(root / 'browser-failure.png'))
            raise
        finally:
            (root / 'browser-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
            await chromium.close()


def verify(args):
    from testsupport import IsolatedServer
    from case_workflow_thermal_fixture import RUN, GEOMETRY, case_dictionary, thermal_files

    api = IsolatedServer('solver-results-browser', app='solver_results_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    (api.root / 'solver_results_fixture.py').write_text(
        'from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    result = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0, 'native_jobs': {}}

    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def job_stopped(identifier):
        job = api.wait_job(identifier, timeout=200)
        assert job['attempt']['process_stop']['verified'] and job['attempt']['phase'] == 'succeeded', job
        return job

    try:
        with api:
            case = api.create_case('Actual OpenFOAM browser results')
            api.call('GET', api.prefix + '/postprocess/results', expect=404)
            api.call('GET', api.prefix + '/report/results', expect=404)
            initial = api.call('GET', api.prefix + '/caseDict')
            api.call('PUT', api.prefix + '/caseDict', {'content': case_dictionary(initial['content']),
                'expected_revision': initial['dict_revision']})
            for path, content in thermal_files().items():
                api.put(path, content)
            geometry = api.put('A-geo/geometry.py', GEOMETRY)
            if geometry.get('auto_preview_job'):
                job_stopped(geometry['auto_preview_job'])
            current = api.call('GET', api.prefix + '/caseDict')
            flow = api.call('POST', api.prefix + '/workflow-runs', {'target': 'report', 'run_dir': RUN,
                'expected_revision': current['dict_revision'], 'parameters': {'geometry_mode': 'export', 'report_mode': 'compose'},
                'budget': {'timeout_seconds': 180, 'max_log_bytes': 4*1024*1024}}, extra_headers={'Idempotency-Key': str(uuid.uuid4())})
            run_path = '/workflow-runs/' + flow['id']

            for stage in ('B', 'C', 'D', 'E', 'report'):
                timeout = time.monotonic() + 200
                while time.monotonic() < timeout:
                    flow = api.call('GET', run_path)
                    assert flow['status'] not in ('failed', 'cancelled', 'interrupted'), flow
                    if stage == 'report' and flow['status'] == 'succeeded':
                        break
                    step = next((step for step in flow['steps'] if step['key'] == 'confirm_' + stage), None)
                    if step and step['status'] == 'awaiting_confirmation':
                        api.call('POST', api.prefix + '/caseDict:advance', {'state': stage, 'confirmed': True,
                            'expected_revision': step['confirmation_revision'], 'workflow_step_id': step['id'],
                            'expected_step_version': step['version']})
                        print(f'SOLVER_RESULTS_CONFIRMED_{stage}', flush=True)
                        break
                    time.sleep(.15)
                else:
                    raise TimeoutError('Workflow did not reach ' + stage)

            for step in flow['steps']:
                if step['attempts']:
                    job = job_stopped(step['attempts'][-1]['job_id'])
                    publication = job['attempt']['outputs']['publication']
                    assert publication['status'] == 'committed'
                    result['native_jobs'][step['key']] = {'job_id': job['id'], 'publication_id': publication['id'], 'verified_stop': True}
            solver = result['native_jobs']['solve']
            solved = api.call('GET', api.prefix + '/solve/results')
            assert solved['publication']['id'] == solver['publication_id']
            assert solved['evidence']['summary']['last_time'] == 1
            job = api.call('GET', f"/jobs/{solver['job_id']}")
            log_endpoint = f"/jobs/{solver['job_id']}/log?offset=0"
            log = api.call('GET', log_endpoint)
            assert 'laplacianFoam' in log['chunk'] and 'Initial residual' in log['chunk']
            assert job['parsed_summary']['summary']['last_time'] == 1
            parsed_path = '/logs/parse?path=' + quote(job['log_path'], safe='')
            assert api.call('GET', api.prefix + parsed_path) == job['parsed_summary']
            (api.root / 'openfoam.log').write_text(log['chunk'], encoding='utf8')
            post = api.call('GET', api.prefix + '/postprocess/results')
            assert post['publication']['id'] == result['native_jobs']['postprocess']['publication_id']
            assert post['metrics']['readiness']['report_ready'] and post['metrics']['readiness']['can_claim_verification']
            metric = next(metric for metric in post['metrics']['metrics'] if metric['name'] == 'mean.T@1')
            assert math.isclose(metric['value'], 350, abs_tol=1e-6)
            artifacts = {}
            for entry in post['manifest']['artifacts']:
                data = api.call('GET', api.prefix + '/raw?path=' + quote(entry['path'], safe=''), raw=True)
                assert digest(data) == entry['sha256'] and len(data) == entry['size']
                artifacts[Path(entry['path']).name] = data
            assert len(artifacts['profile.csv'].splitlines()) == 101, 'Expected the default 100 line-sample points'
            result['postprocessing'] = {'mean': metric, 'artifacts': sorted(artifacts)}

            report = api.call('GET', api.prefix + '/report/results')
            report_id = report['publication']['id']
            assert report_id == result['native_jobs']['report']['publication_id'] and not report['publication']['stale']
            hashes = {}
            report_endpoints = []
            for suffix in ('html', 'docx'):
                path = 'E-report/build/report.' + suffix
                endpoint = api.prefix + '/report/artifact?publication_id=' + report_id + '&path=' + quote(path, safe='')
                report_endpoints.append(endpoint)
                data = api.call('GET', endpoint, raw=True)
                hashes[suffix] = digest(data)
                assert hashes[suffix] == report['manifest']['artifacts'][path]['sha256']

            foreign = api.call('POST', '/auth/register', {'email': str(uuid.uuid4()) + '@example.com',
                'password': 'isolated-test-access', 'name': 'Separate organization'})
            for endpoint in (log_endpoint, api.prefix + '/postprocess/results', *report_endpoints):
                api.call('GET', endpoint, expect=404, extra_headers={'Authorization': 'Bearer ' + foreign['token']})
            result['foreign_access_denied'] = True
            config = {'root': str(api.root), 'base': api.base, 'token': api.token, 'channel': args.channel,
                'case': case['name'], 'solver_job': solver['job_id'], 'post_dir': post['result_dir'],
                'csv_columns': len(artifacts['profile.csv'].splitlines()[0].split(b',')), 'csv_rows': 100,
                'report_id': report_id, 'report_hashes': hashes}
            browser_tmp = api.root / 'browser-tmp'
            browser_tmp.mkdir()
            browser_env = dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp))
            print('SOLVER_RESULTS_PUBLISHED_ENTERING_BROWSER', flush=True)
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(config), text=True, check=True, timeout=190, env=browser_env)
            with zipfile.ZipFile(api.root / 'report.docx') as document:
                body = document.read('word/document.xml')
                assert b'mean.T@1' in body and str(metric['value']).encode('ascii') in body
            result['browser'] = json.loads((api.root / 'browser-result.json').read_text(encoding='utf8'))
        result['passed'] = api.proc.poll() is not None
    finally:
        result['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'solver-results-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print('SOLVER_RESULTS_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
