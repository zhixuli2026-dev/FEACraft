"""Actual CadQuery topology, case-owned declarations, and pinned browser evidence."""

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
from urllib.parse import urlsplit


OPEN = 'import cadquery as cq\ndef build_geometry(ctx):\n    return {"open": cq.Face.makePlane(4, 5)}\n'
CLOSED = 'import cadquery as cq\ndef build_geometry(ctx):\n    return {"solid": cq.Workplane("XY").box(1, 2, 3)}\n'
DECLARATIONS = {'sourceType': 'parametricCAD', 'topology': 'singleRegion', 'scaleToMeters': '0.001',
                'symmetryUsed': 'true', 'periodicUsed': 'true',
                'watertightRequired': 'true', 'manifoldRequired': 'true'}


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    prefix = f"/api/v1/cases/{settings['case_id']}"
    base = settings['base']
    headers = {'Authorization': 'Bearer ' + settings['token']}
    validation_route = prefix + '/geometry/validation'
    report = {}
    async with async_playwright() as driver:
        chromium = await driver.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1450, 'height': 935})
        await context.add_init_script('if(location.origin===' + json.dumps(base) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(20000)
        expect.set_options(timeout=25000)
        await page.goto(base)
        await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
        disclosure = page.locator('summary').filter(has_text=re.compile(r'^几何验证$'))
        if not await disclosure.is_visible():
            await page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^几何预览$')).click()
        verification = page.get_by_role('region', name='几何验证记录')

        async def read(button):
            async with page.expect_response(lambda response: urlsplit(response.url).path == validation_route
                and response.request.method == 'GET') as result:
                await button.click()
            response = await result.value
            assert response.status == 200, await response.text()
            return await response.json()

        before = await read(disclosure)
        assert before['publication']['id'] == settings['open_publication']
        await expect(verification).to_contain_text('实测水密 未通过 · 实测流形 通过')
        await expect(verification).to_contain_text('边界边 4')
        for visible in ('parametricCAD', 'singleRegion', '到米比例', '0.001', '采用对称', '采用周期',
                        '要求水密', '要求流形', before['source_sha256']):
            await expect(verification).to_contain_text(visible)
        await page.screenshot(path=str(root / 'open-geometry-evidence.png'))

        source = await context.request.put(base + prefix + '/file', headers=headers,
            data={'path': 'A-geo/geometry.py', 'content': CLOSED})
        assert source.status == 200, await source.text()
        preview = (await source.json())['auto_preview_job']
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            response = await context.request.get(base + f'/api/v1/jobs/{preview}', headers=headers)
            assert response.status == 200
            job = await response.json()
            if job['status'] not in ('queued', 'running'):
                assert job['status'] == 'succeeded' and job['exit_code'] == 0
                break
            await asyncio.sleep(.1)
        else:
            raise TimeoutError('Native CadQuery closed-shape preview did not finish')
        original = await read(verification.get_by_role('button', name='重新核对原验证'))
        assert original['publication']['id'] == before['publication']['id'] and original['publication']['stale']
        assert original['summary'] == before['summary']
        await expect(verification).to_contain_text(re.compile('历史发布|保留产物'))
        latest = await read(verification.get_by_role('button', name='读取最新验证'))
        assert latest['publication']['id'] != original['publication']['id'] and not latest['publication']['stale']
        await expect(verification).to_contain_text('实测水密 通过 · 实测流形 通过')
        await expect(verification).to_contain_text('边界边 0')
        report.update(open_publication=before['publication']['id'], closed_publication=latest['publication']['id'],
                      closed_preview_job=preview, stale_original_readable=True)
        await page.screenshot(path=str(root / 'closed-geometry-evidence.png'))
        (root / 'browser-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        await chromium.close()


def nonmanifold_evidence():
    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from app.services.geo_validation import topology_requirement_reason, validate_shape

    sewing = BRepBuilderAPI_Sewing(.001, True, True, True, True)
    for coords in (
        [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
        [(1, 0, 0), (0, 0, 0), (0, 0, 1)],
        [(0, 0, 0), (1, 0, 0), (0, -1, 0)],
    ):
        face = cq.Face.makeFromWires(cq.Wire.makePolygon([cq.Vector(*point) for point in coords], close=True))
        sewing.Add(face.wrapped)
    sewing.Perform()
    evidence = validate_shape(cq.Shape.cast(sewing.SewedShape()))
    assert not evidence['manifold'] and evidence['topology']['nonmanifold_edges'] == 1, evidence
    assert topology_requirement_reason({'manifoldRequired': True}, evidence)
    return evidence


def verify(args):
    from app.services.casedict import set_block
    from testsupport import IsolatedServer

    api = IsolatedServer('geometry-declarations-browser', app='a17_fixture:app')
    bundle = args.dist.resolve(strict=True)
    (api.root / 'a17_fixture.py').write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temp = api.root / 'browser-tmp'
    temp.mkdir()
    result = {'passed': False, 'model_calls': 0, 'backend': 'fs/sqlite'}
    try:
        with api:
            result['native_nonmanifold'] = nonmanifold_evidence()
            case = api.create_case('A17 geometry declarations')
            prefix, token = api.prefix, api.token
            path = prefix + '/caseDict'
            initial = api.put('A-geo/geometry.py', OPEN)
            api.wait_job(initial['auto_preview_job'])
            legacy = api.call('GET', prefix + '/geometry/validation')
            assert legacy['summary']['manifold'] and not legacy['summary']['watertight']
            assert legacy['declared_geometry'] == {}
            assert api.advance('B')['workflow_state'] == 'B'
            api.advance('A')
            original = api.call('GET', path)
            text = set_block(original['content'], 'geometry', DECLARATIONS)
            api.call('PUT', path, {'content': text, 'expected_revision': original['dict_revision']})
            open_job = api.call('POST', prefix + '/geometry:preview', {})
            api.wait_job(open_job['job_id'])
            validation = api.call('GET', prefix + '/geometry/validation')
            assert validation['declared_geometry'] == DECLARATIONS
            assert validation['source_sha256'] == hashlib.sha256(OPEN.encode()).hexdigest()
            current = api.call('GET', path)
            blocked = api.advance('B', expect=409)
            assert '水密' in blocked['detail'] and '流形' not in blocked['detail']
            assert api.call('GET', path) == current
            result['open_shape_declared_gate'] = {'job_id': open_job['job_id'],
                'declared_geometry': validation['declared_geometry'], 'actual': validation['summary'],
                'blocked': blocked['detail'], 'first_unrestricted_legacy_shape_passed': True}

            settings = {'root': str(api.root), 'base': api.base, 'token': token, 'case': case['name'],
                        'case_id': case['id'], 'channel': args.channel, 'open_publication': validation['publication']['id']}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=170,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            result['browser'] = json.loads((api.root / 'browser-result.json').read_text(encoding='utf8'))
            exported = api.call('POST', prefix + '/geometry:export', {})
            api.wait_job(exported['job_id'])
            final = api.call('GET', prefix + '/geometry/validation')
            assert final['declared_geometry'] == DECLARATIONS and final['summary']['watertight']
            assert final['objects'][0]['volume'] == 6 and final['source_sha256'] == hashlib.sha256(CLOSED.encode()).hexdigest()
            assert api.advance('B')['workflow_state'] == 'B'
            api.create_case('Another A17 owner')
            api.call('GET', prefix + '/geometry/validation?publication_id=' + validation['publication']['id'], expect=404)
            api.token = token
            assert api.call('GET', prefix)['workflow_state'] == 'B'
            result['closed_export_then_b'] = {'job_id': exported['job_id'], 'pub': final['publication'],
                'closed_solid_volume': final['objects'][0]['volume'], 'cross_account_404': True}
        result['passed'] = True
    finally:
        (api.root / 'geometry-declarations-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print('GEOMETRY_DECLARATIONS_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce
        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
