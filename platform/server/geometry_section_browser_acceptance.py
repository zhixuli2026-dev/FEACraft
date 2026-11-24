"""Inspect actual CadQuery sections, caps and source restoration in Edge."""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time


CASE = "Concave CAD section"
EMPTY = "Empty CAD section"
PLATE = "concave perforated plate"
DENSE = "dense perforated plate"
OPEN = "open perforated plate"
SCRIPT = """import cadquery as cq

def extrude(points, height):
    return cq.Workplane('XY').polyline(points).close().extrude(height).translate((0,0,-height/2))

def build_geometry(ctx):
    perimeter = [(-8,-6),(8,-6),(8,6),(3,6),(3,2),(-3,2),(-3,6),(-8,6)]
    left = [(-7,-4),(-4,-4),(-4,-1),(-5,-1),(-5,-3),(-6,-3),(-6,-1),(-7,-1)]
    right = [(4,-2),(7,-2),(7,2),(4,2)]
    body = extrude(perimeter,4).cut(extrude(left,6)).cut(extrude(right,6))
    assert body.val().isValid() and abs(body.val().Volume()-596)<1e-7
    return {'concave perforated plate':body}
"""

# Read the Three.js objects attached to the actual mounted component. No test
# changes the production scene references or constructs substitute clip meshes.
RENDERED_GEOMETRY = """([name, axis]) => {
  const host = document.querySelector('[data-testid=geo-canvas-host]');
  let fiber = host[Object.keys(host).find(key => key.startsWith('__reactFiber$'))];
  let scene, renderer;
  while (fiber && (!scene || !renderer)) {
    if (typeof fiber.type === 'function') {
      for (let hook = fiber.memoizedState; hook; hook = hook.next) {
        const current = hook.memoizedState?.current;
        if (current?.isScene) scene = current;
        if (current?.isWebGLRenderer) renderer = current;
      }
    }
    fiber = fiber.return;
  }
  if (!scene || !renderer) throw new Error('Mounted Three.js scene or renderer missing');
  const model = scene.children.find(child => child.isMesh && child.userData.vfMesh === name && !child.userData.clipCap);
  const cap = scene.children.find(child => child.isMesh && child.userData.vfMesh === name && child.userData.clipCap);
  const helper = scene.children.find(child => child.isMesh && child.geometry.type === 'PlaneGeometry'
    && child.material.color?.getHex() === 0x2266aa);
  const geometry = mesh => {
    if (!mesh) return null;
    const positions = mesh.geometry.getAttribute('position').array;
    const indices = mesh.geometry.index.array;
    let min = Infinity, max = -Infinity;
    for (let point = axis; point < positions.length; point += 3) {
      min = Math.min(min, positions[point]);
      max = Math.max(max, positions[point]);
    }
    return {uuid: mesh.geometry.uuid, source: mesh.userData.sourceGeometry.uuid, points: positions.length/3,
            triangles: indices.length/3, min, max};
  };
  let capStats = geometry(cap);
  if (capStats) {
    const p = cap.geometry.getAttribute('position').array;
    const i = cap.geometry.index.array;
    let area = 0, normalSign = 0, wrongNormals = 0;
    for (let t = 0; t < i.length; t += 3) {
      const a = 3*i[t], b = 3*i[t+1], c = 3*i[t+2];
      const u = [p[b]-p[a],p[b+1]-p[a+1],p[b+2]-p[a+2]];
      const v = [p[c]-p[a],p[c+1]-p[a+1],p[c+2]-p[a+2]];
      const n = [u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];
      area += Math.hypot(...n)/2;
      if (Math.abs(n[axis]) < 1e-10) continue;
      if (!normalSign) normalSign = Math.sign(n[axis]);
      if (Math.sign(n[axis]) !== normalSign) wrongNormals++;
    }
    capStats = {...capStats, area, normalSign, wrongNormals};
  }
  let plane = null;
  if (helper) {
    const {x,y,z,w} = helper.quaternion;
    plane = {coordinate: helper.position.getComponent(axis), opacity: helper.material.opacity,
      orientation: [2*(x*z+w*y),2*(y*z-w*x),1-2*(x*x+y*y)]};
  }
  return {model: geometry(model), cap: capStats, plane, geometries: renderer.info.memory.geometries};
}"""


def write_variant(path, model):
    payload = json.dumps({'objects': [model]}, separators=(',', ':')).encode('utf8')
    path.write_bytes(base64.b64encode(payload))


def subdivide(model):
    """Densify the actual CAD triangulation while preserving its faces and holes."""
    vertices, faces = [], []
    source_points, source_faces = model['vertices'], model['faces']
    divisions = math.isqrt(180000 // len(source_faces)) - 1
    for face in source_faces:
        a, b, c = [source_points[index] for index in face]
        indices = {}
        for row in range(divisions + 1):
            for col in range(divisions + 1 - row):
                indices[row, col] = len(vertices)
                vertices.append([round((x*(divisions-row-col)+y*row+z*col)/divisions, 8)
                                 for x, y, z in zip(a, b, c)])
        for row in range(divisions):
            for col in range(divisions - row):
                faces.append([indices[row,col], indices[row+1,col], indices[row,col+1]])
                if row + col < divisions - 1:
                    faces.append([indices[row+1,col], indices[row+1,col+1], indices[row,col+1]])
    assert 100000 <= len(faces) <= 200000 and len(vertices) <= 100000
    return {**model, 'name': DENSE, 'vertices': vertices, 'faces': faces}


async def browser(data):
    from playwright.async_api import async_playwright, expect

    root, base = Path(data['root']), data['base']
    report = {'checks': []}
    errors = []
    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=data['channel'], args=['--enable-unsafe-swiftshader'])
        context = await edge.new_context(viewport={'width': 1536, 'height': 1120}, locale='zh-CN')
        await context.add_init_script(
            'if (location.origin === ' + json.dumps(base) + ') localStorage.setItem("vf_token", '
            + json.dumps(data['token']) + ');')
        page = await context.new_page()
        page.on('pageerror', lambda event: errors.append(str(event)))
        page.set_default_timeout(30000)
        cdp = await context.new_cdp_session(page)
        host = page.get_by_test_id('geo-canvas-host')
        canvas = host.locator('canvas')
        tree = page.get_by_test_id('cad-tree')
        axis_control = page.get_by_role('combobox', name='剖切轴', exact=True)
        side_control = page.get_by_role('combobox', name='保留侧', exact=True)
        slider = page.get_by_role('slider', name='切面位置', exact=True)

        async def settle():
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')

        async def activate(case):
            row = page.locator('[data-case-name=' + json.dumps(case) + ']')
            await row.click()
            await expect(row).to_have_attribute('aria-pressed', 'true')
            if not await host.is_visible():
                await page.locator('.dv-tab:visible').filter(has_text=re.compile('^几何预览$')).click()
            if not await tree.is_visible():
                await page.get_by_role('button', name='对象与设置', exact=True).click()
            await expect(tree).to_be_visible()

        async def inspect(name, axis=2):
            await settle()
            return await page.evaluate(RENDERED_GEOMETRY, [name, axis])

        async def apply_section(axis, side, name=PLATE, position=.5):
            await axis_control.select_option('xyz'[axis])
            await side_control.select_option(side)
            result = await inspect(name, axis)
            assert result['model'] and result['plane'], result
            normal_sign = 1 if side == 'lower' else -1
            model, cap, plane = result['model'], result['cap'], result['plane']
            assert abs(plane['orientation'][axis] + normal_sign) < 1e-7 and 0 < plane['opacity'] < 1
            extent = (16, 12, 4)[axis]
            coordinate = (position - .5) * extent
            assert abs(plane['coordinate'] - coordinate) < 1e-5, result
            boundary = model['max'] if side == 'lower' else model['min']
            assert abs(boundary - coordinate) < 1e-5 and model['triangles'] > 0
            if cap:
                assert abs(cap['min'] - coordinate) < 1e-5 and abs(cap['max'] - coordinate) < 1e-5
                assert cap['normalSign'] == normal_sign and cap['wrongNormals'] == 0
            return result

        async def load_variant(basename, name):
            encoded = (root / basename).read_text(encoding='ascii')
            await page.evaluate('text => window.vfLoadSceneFromBase64(text)', encoded)
            await expect(tree.get_by_role('button', name=name, exact=True)).to_be_visible()

        async def heap():
            await cdp.send('HeapProfiler.collectGarbage')
            return (await cdp.send('Runtime.getHeapUsage'))['usedSize']

        try:
            await page.goto(base, wait_until='domcontentloaded')
            await activate(CASE)
            await expect(tree.get_by_role('button', name=PLATE, exact=True)).to_be_visible()
            await expect(canvas).to_be_visible()
            await expect(axis_control).to_have_value('')
            await expect(side_control).to_be_disabled()
            await expect(slider).to_be_disabled()
            original = await inspect(PLATE)
            assert original['model']['source'] == original['model']['uuid'] and not original['cap'] and not original['plane']
            original_png = await canvas.screenshot(path=root/'section-original.png')
            checks = report['checks']
            report['sections'] = {}
            for axis, area in enumerate((32, 52, 149)):
                for side in ('lower', 'higher'):
                    section = await apply_section(axis, side)
                    assert section['cap'] and abs(section['cap']['area'] - area) < 1e-4, (area, section)
                    await expect(host).to_have_attribute('data-clip-caps', '1')
                    report['sections']['xyz'[axis]+':'+side] = section
            checks.append('three real CAD axes, both sides, exact cap triangles and outward winding')

            await side_control.select_option('lower')
            await slider.focus()
            await expect(slider).to_be_focused()
            await slider.press('ArrowRight')
            await expect(slider).to_have_attribute('aria-valuetext', '51%')
            shifted = await apply_section(2, 'lower', position=.51)
            assert abs(shifted['cap']['area'] - 149) < 1e-4
            await page.screenshot(path=root/'section-controls.png')
            section_png = await canvas.screenshot(path=root/'section-holes.png')
            assert section_png != original_png
            report['shifted_section'] = shifted
            checks.append('keyboard moves visible helper and actual intersection together')

            await axis_control.select_option('')
            await expect(side_control).to_be_disabled()
            await expect(slider).to_be_disabled()
            restored = await inspect(PLATE)
            assert restored['model'] == original['model'] and not restored['cap'] and not restored['plane']
            assert await canvas.screenshot(path=root/'section-restored.png') == original_png
            checks.append('turning section off restores the exact source geometry and canvas')

            await load_variant('open.b64', OPEN)
            opened = await apply_section(2, 'lower', OPEN, position=.51)
            assert not opened['cap']
            await expect(tree.get_by_role('status').filter(has_text='截面轮廓未闭合')).to_be_visible()
            await expect(host).to_have_attribute('data-clip-caps', '0')
            report['open_section'] = opened
            await page.screenshot(path=root/'section-open.png')
            await axis_control.select_option('')
            await expect(tree.get_by_role('status').filter(has_text='截面轮廓未闭合')).to_have_count(0)
            checks.append('open outer boundary retains clipped body and closed holes remain unfilled')

            await load_variant('dense.b64', DENSE)
            dense = await inspect(DENSE)
            assert dense['model']['triangles'] >= 100000
            before_heap = await heap()
            measurements = []
            for _ in range(3):
                started = time.perf_counter()
                large_cut = await apply_section(2, 'lower', DENSE, position=.51)
                assert large_cut['cap'] and abs(large_cut['cap']['area'] - 149) < 1e-3, large_cut
                await axis_control.select_option('')
                restored = await inspect(DENSE)
                measurements.append(round(time.perf_counter() - started, 3))
                assert restored['model'] == dense['model'] and not restored['cap'] and not restored['plane']
                assert restored['geometries'] <= dense['geometries'], (dense, restored)
            after_heap = await heap()
            assert after_heap - before_heap < 48 * 1024 * 1024, (before_heap, after_heap)
            report['dense_section'] = {'source': dense, 'cut_and_restore_seconds': measurements,
                'heap_before': before_heap, 'heap_after': after_heap}
            checks.append('over 100,000 CAD-derived facets repeatedly clip and restore without retained GL meshes')

            await page.evaluate('() => window.vfLoadSceneFromBase64("invalid")')
            await expect(page.get_by_test_id('geo-status')).to_contain_text('保留上一有效场景')
            assert (await inspect(DENSE))['model'] == dense['model']
            await apply_section(2, 'lower', DENSE, position=.51)
            await activate(EMPTY)
            await expect(axis_control).to_be_disabled()
            cleared = await inspect(DENSE)
            assert not cleared['model'] and not cleared['cap'] and not cleared['plane']
            report.update(empty_scene=cleared, browser=edge.version, page_errors=errors)
            assert not errors, errors
            checks.append('invalid input holds current mesh; empty case removes previous helper and geometry')
            report['passed'] = True
        except Exception as error:
            report.update(passed=False, error=str(error)[:2500], page_errors=errors)
            await page.screenshot(path=root/'section-failure.png')
            raise
        finally:
            (root/'section-browser-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer('geometry-section-browser', app='section_browser_fixture:app', access_log=True)
    root = api.root
    print('GEOMETRY_SECTION_BROWSER_EVIDENCE', root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    (root/'section_browser_fixture.py').write_text(
        'from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n", encoding='utf8')
    browser_tmp = root/'browser-tmp'
    browser_tmp.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        primitive = subprocess.run(['node', str(Path(__file__).resolve().parents[1] / 'tools/cad-clip-smoke.mjs')],
            capture_output=True, text=True, encoding='utf8', timeout=30)
        (root/'clip-primitive.log').write_text(primitive.stdout + primitive.stderr, encoding='utf8')
        primitive.check_returncode()
        with api:
            case = api.create_case(CASE)
            workspace = api.call('GET', api.prefix)['workspace_id']
            api.call('POST', f"/workspaces/{workspace}/cases", {'name': EMPTY})
            saved = api.put('A-geo/geometry.py', SCRIPT)
            job = api.wait_job(saved['auto_preview_job'])
            assert job['exit_code'] == 0 and job['attempt']['process_stop']['verified']
            published = api.call('GET', api.prefix + '/geometry/scene')
            valid = api.call('GET', api.prefix + '/geometry/validation')
            source, = published['objects']
            solid, = valid['objects']
            assert source['name'] == PLATE and solid['valid'] and abs(solid['volume'] - 596) < 1e-6
            open_faces = [face for face in source['faces']
                          if not all(abs(source['vertices'][index][0] - 8) < 1e-6 for index in face)]
            assert 0 < len(open_faces) < len(source['faces'])
            write_variant(root/'open.b64', {**source, 'name': OPEN, 'faces': open_faces})
            dense = subdivide(source)
            write_variant(root/'dense.b64', dense)
            report.update(source_job=job['id'], publication=published['publication']['id'],
                original_faces=len(source['faces']), dense_faces=len(dense['faces']), dense_vertices=len(dense['vertices']))
            before = hashlib.sha256(json.dumps(published, sort_keys=True).encode()).hexdigest()
            data = {'root': str(root), 'base': api.base, 'token': api.token, 'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(data), text=True, check=True, timeout=300,
                env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
            assert hashlib.sha256(json.dumps(api.call('GET', api.prefix + '/geometry/scene'), sort_keys=True).encode()).hexdigest() == before
            api.create_case('Outside CAD section owner')
            api.call('GET', f"/cases/{case['id']}/geometry/scene", expect=404)
            report.update(scene_sha256=before, foreign_status=404)
        assert api.proc.poll() is not None
        report['passed'] = True
    finally:
        (root/'section-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')


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
        main(parser.parse_args())
