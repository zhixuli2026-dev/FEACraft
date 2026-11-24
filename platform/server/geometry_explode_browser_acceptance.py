"""Exercise timed CAD explosion and motion ownership on actual Edge geometry."""

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


CASE = "Nested CAD explosion"
EMPTY = "Empty CAD explosion"
PARTS = {'rotor', 'bearing', 'shaft', 'bracket', 'carrier', 'fastener', 'reference'}
SCRIPT = """import cadquery as cq

def build_geometry(ctx):
    block = cq.Workplane('XY').box(3, 5, 2)
    cylinder = cq.Workplane('XY').circle(1.4).extrude(3)
    axis = cq.Vector(0, 0, 1)
    drive = cq.Assembly(name='drive')
    drive.add(block, name='rotor', loc=cq.Location(cq.Vector(3, 4, 1)))
    drive.add(block, name='bearing', loc=cq.Location(cq.Vector(-1, 7, 5), axis, 90))
    drive.add(cylinder, name='shaft', loc=cq.Location(cq.Vector(2, 9, 10)))
    support = cq.Assembly(name='support')
    support.add(block, name='bracket', loc=cq.Location(cq.Vector(2, 3, 8)))
    support.add(block, name='carrier', loc=cq.Location(cq.Vector(-4, 1, -2), axis, 45))
    support.add(cylinder, name='fastener', loc=cq.Location(cq.Vector(5, -2, 3)))
    station = cq.Assembly(name='station')
    station.add(drive, loc=cq.Location(cq.Vector(10, 20, 2), axis, 90))
    station.add(support, loc=cq.Location(cq.Vector(-10, 1, 4)))
    reference = cq.Workplane('XY').box(6, 3, 3).translate((-4, -5, 1))
    return {'station': station, 'reference': reference}
"""

TRACK = {'schema': 'vibeflow.cadquery.animation.v1', 'speed': 1, 'repeat': True,
         'tracks': [{'selector': 'rotor', 'action': 'tx', 'times': [0, 3], 'values': [0, 6]}]}

# Observe the actual mounted Three.js meshes and the viewer clock in Edge.
# Only UI controls change the scene and the clock.
VIEW_STATE = """() => {
  const host = document.querySelector('[data-testid=geo-canvas-host]');
  let fiber = host[Object.keys(host).find(key => key.startsWith('__reactFiber$'))];
  let scene, motion;
  while (fiber && (!scene || !motion)) {
    if (typeof fiber.type === 'function') {
      for (let hook = fiber.memoizedState; hook; hook = hook.next) {
        const current = hook.memoizedState?.current;
        if (current?.isScene) scene = current;
        if (current && typeof current.duration === 'number' && typeof current.from === 'number'
            && typeof current.elapsed === 'number' && typeof current.easing === 'string') motion = current;
      }
    }
    fiber = fiber.return;
  }
  if (!scene || !motion) throw new Error('Mounted CAD scene or explosion clock missing');
  const parts = {};
  for (const mesh of scene.children) {
    if (!mesh.isMesh || !mesh.userData.vfMesh || mesh.userData.clipCap) continue;
    const node = mesh.userData;
    const edge = scene.children.find(child => child.isLineSegments && child.userData.vfMesh === node.vfMesh);
    parts[node.vfMesh] = {position: mesh.position.toArray(), rotation: mesh.rotation.toArray().slice(0,3),
      edgePosition: edge.position.toArray(), home: node.home.toArray(), center: node.center.toArray(),
      bbox: node.bbox, geometry: mesh.geometry.uuid};
  }
  return {pieces: parts, motion: {...motion}, timestamp: performance.now()};
}"""


def close_vector(actual, expected):
    return all(math.isclose(a, b, abs_tol=2e-6) for a, b in zip(actual, expected, strict=True))


def verify_offset(state, baseline, progress):
    min_corner = [min(piece['bbox']['min'][axis] for piece in baseline.values()) for axis in range(3)]
    max_corner = [max(piece['bbox']['max'][axis] for piece in baseline.values()) for axis in range(3)]
    center = [(low+high)/2 for low, high in zip(min_corner, max_corner, strict=True)]
    assert state['pieces'].keys() == baseline.keys()
    for name, piece in state['pieces'].items():
        original = baseline[name]
        vector = [part - origin for part, origin in zip(original['center'], center, strict=True)]
        distance = math.hypot(*vector)
        assert distance > 0
        expected = [start + component/distance * progress * 50
                    for start, component in zip(original['home'], vector, strict=True)]
        assert close_vector(piece['position'], expected), (name, piece['position'], expected)
        assert close_vector(piece['edgePosition'], expected)
        assert piece['rotation'] == original['rotation'] and piece['geometry'] == original['geometry']


async def browser(data):
    from playwright.async_api import async_playwright, expect

    root, base = Path(data['root']), data['base']
    report, errors = {'checks': []}, []
    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=data['channel'], args=['--enable-unsafe-swiftshader'])
        context = await edge.new_context(viewport={'width': 1536, 'height': 1120},
            locale='zh-CN', reduced_motion='no-preference')
        await context.add_init_script(
            'if (location.origin === ' + json.dumps(base) + ') localStorage.setItem("vf_token", '
            + json.dumps(data['token']) + ');')
        page = await context.new_page()
        page.on('pageerror', lambda event: errors.append(str(event)))
        page.set_default_timeout(30000)
        host = page.get_by_test_id('geo-canvas-host')
        tree = page.get_by_test_id('cad-tree')
        explode = tree.get_by_role('checkbox', name='爆炸', exact=True)
        duration = tree.get_by_role('spinbutton', name='时长（秒）', exact=True)
        easing = tree.get_by_role('combobox', name='缓动', exact=True)
        amount = tree.get_by_role('slider', name='分离程度', exact=True)
        pause = tree.get_by_role('button', name='暂停过渡', exact=True)
        resume = tree.get_by_role('button', name='继续过渡', exact=True)
        ordinary = tree.get_by_test_id('cad-anim')
        track_play = tree.get_by_test_id('cad-track-play')
        status = report['checks']

        async def activate(case):
            row = page.locator('[data-case-name=' + json.dumps(case) + ']')
            await row.click()
            await expect(row).to_have_attribute('aria-pressed', 'true')
            if not await host.is_visible():
                await page.locator('.dv-tab:visible').filter(has_text=re.compile('^几何预览$')).click()
            if not await tree.is_visible():
                await page.get_by_role('button', name='对象与设置', exact=True).click()
            await expect(tree).to_be_visible()

        async def view():
            return await page.evaluate(VIEW_STATE)

        async def expect_phase(phase):
            await expect(host).to_have_attribute('data-explode-phase', phase)

        async def expect_progress(value):
            await expect(host).to_have_attribute('data-explode-progress', f'{value:.3f}')

        async def intermediate(lower=.14, upper=.7):
            # Choose a sampled frame while the visible transition is still active.
            async with asyncio.timeout(6):
                while True:
                    state = await view()
                    motion = state['motion']
                    if motion['phase'] == 'playing' and lower < motion['elapsed']/motion['duration'] < upper:
                        return state
                    await asyncio.sleep(.025)

        async def pixels(path):
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            return await host.locator('canvas').screenshot(path=path)

        try:
            await page.goto(base, wait_until='domcontentloaded')
            await activate(CASE)
            await tree.get_by_role('combobox', name='对象树视图').select_option('assembly')
            for part in PARTS:
                await expect(tree.get_by_role('button', name=part, exact=True)).to_be_visible()
            model = (await view())['pieces']
            assert set(model) == PARTS
            verify_offset(await view(), model, 0)
            original_image = await pixels(root/'explosion-source.png')
            await expect(pause).to_be_disabled()

            await duration.fill('2')
            await easing.select_option('linear')
            await explode.check()
            first = await intermediate()
            timeline = first['motion']
            assert timeline['duration'] == 2 and timeline['from'] == 0 and timeline['target'] == .5
            assert abs(timeline['current'] - .5*timeline['elapsed']/2) < 1e-8
            verify_offset(first, model, timeline['current'])
            await pause.click()
            await expect_phase('paused')
            paused = await view()
            assert 0 < paused['motion']['current'] < .5
            await asyncio.sleep(.4)
            held = await view()
            assert held['motion'] == paused['motion'] and held['pieces'] == paused['pieces']
            await duration.fill('0')
            await expect(duration).to_have_attribute('aria-invalid', 'true')
            await expect(tree.get_by_role('status').filter(has_text='继续使用上次有效时长')).to_be_visible()
            assert (await view())['motion']['duration'] == 2
            await page.screenshot(path=root/'explosion-paused-controls.png')
            exploded_image = await pixels(root/'explosion-paused.png')
            assert exploded_image != original_image
            await resume.click()
            await expect_phase('playing')
            await expect_progress(.5)
            await expect_phase('idle')
            ended = await view()
            assert ended['motion']['elapsed'] == 2
            verify_offset(ended, model, .5)
            report['linear_pause_resume'] = {'first_elapsed': timeline['elapsed'],
                'first_offset': timeline['current'], 'paused_elapsed': paused['motion']['elapsed'],
                'actual_pause_interval_seconds': (held['timestamp']-paused['timestamp'])/1000,
                'completed_elapsed': ended['motion']['elapsed'], 'invalid_input_kept_duration': 2}
            status.append('editable fixed duration, physical interpolation, pause freezes all parts and resume continues')

            await duration.fill('1.8')
            await expect(duration).to_have_attribute('aria-invalid', 'false')
            await easing.select_option('ease-in-out')
            await explode.uncheck()
            eased = await intermediate()
            motion = eased['motion']
            assert motion['duration'] == 1.8 and motion['from'] == .5 and motion['target'] == 0
            t = motion['elapsed']/motion['duration']
            assert abs(motion['current'] - .5*(1-t*t*(3-2*t))) < 1e-8
            verify_offset(eased, model, motion['current'])
            await ordinary.check()
            await expect_progress(0)
            await expect_phase('idle')
            await expect(explode).not_to_be_checked()
            async with asyncio.timeout(4):
                while True:
                    normal = await view()
                    if normal['pieces']['rotor']['rotation'] != model['rotor']['rotation']:
                        break
                    await asyncio.sleep(.025)
            assert all(part['position'] == part['home'] for part in normal['pieces'].values()), normal
            await ordinary.uncheck()
            verify_offset(await view(), model, 0)
            report['ease_in_out'] = {'time_ratio': t, 'collapse_offset': motion['current'],
                'ordinary_started_while_collapsing': True}
            status.append('ease-in-out changes real geometry and ordinary animation takes ownership during collapse')

            await easing.select_option('ease-out')
            await explode.check()
            decelerating = await intermediate()
            motion = decelerating['motion']
            t = motion['elapsed']/motion['duration']
            assert abs(motion['current'] - .5*(1-(1-t)**3)) < 1e-8
            verify_offset(decelerating, model, motion['current'])
            await amount.focus()
            await amount.press('ArrowRight')
            await expect(amount).to_have_attribute('aria-valuetext', '55%')
            redirected = await intermediate()
            motion = redirected['motion']
            assert 0 < motion['from'] < .5 and motion['target'] == .55
            verify_offset(redirected, model, motion['current'])
            report['ease_out'] = {'redirect_from': motion['from'], 'redirect_target': motion['target']}
            status.append('ease-out applies to the physical pose and keyboard retargeting starts at the current pose')

            await tree.get_by_test_id('cad-track-json').fill(json.dumps(TRACK))
            await tree.get_by_test_id('cad-track-apply').click()
            await expect(tree.get_by_test_id('cad-track-status')).to_contain_text('已应用 1 条轨道')
            await expect_progress(0)
            await track_play.click()
            await expect(host).to_have_attribute('data-track-playing', 'true')
            async with asyncio.timeout(4):
                while True:
                    tracked = await view()
                    if tracked['pieces']['rotor']['position'][0] > model['rotor']['position'][0] + .03:
                        break
                    await asyncio.sleep(.025)
            assert tracked['motion']['phase'] == 'idle' and tracked['motion']['current'] == 0
            await explode.check()
            await expect(host).to_have_attribute('data-track-playing', 'false')
            await expect(host).to_have_attribute('data-track-time', '0.000')
            await expect_phase('playing')
            reactivated = await view()
            verify_offset(reactivated, model, reactivated['motion']['current'])
            await track_play.click()
            await expect(host).to_have_attribute('data-track-playing', 'true')
            await expect_progress(0)
            await expect(explode).not_to_be_checked()
            await tree.get_by_test_id('cad-track-stop').click()
            await expect(host).to_have_attribute('data-track-playing', 'false')
            verify_offset(await view(), model, 0)
            status.append('explosion and real keyframe playback replace each other and restore source poses')

            await explode.check()
            await intermediate()
            await page.emulate_media(reduced_motion='reduce')
            await expect(duration).to_be_disabled()
            await expect(pause).to_be_disabled()
            await expect_progress(.55)
            await expect_phase('idle')
            verify_offset(await view(), model, .55)
            await explode.uncheck()
            await expect_progress(0)
            verify_offset(await view(), model, 0)
            restored_image = await pixels(root/'explosion-restored.png')
            assert restored_image == original_image
            await page.emulate_media(reduced_motion='no-preference')
            animate = tree.get_by_test_id('cad-explode-animate')
            await expect(animate).to_be_enabled()
            await animate.uncheck()
            await explode.check()
            await expect_phase('idle')
            await expect_progress(.55)
            verify_offset(await view(), model, .55)
            await animate.check()
            await explode.uncheck()
            await intermediate()
            await activate(EMPTY)
            await expect(explode).to_be_disabled()
            await expect_phase('idle')
            await expect_progress(0)
            assert not (await view())['pieces']
            await activate(CASE)
            await expect(explode).not_to_be_checked()
            await expect_progress(0)
            await expect(tree.get_by_role('button', name='rotor', exact=True)).to_be_visible()
            assert set((await view())['pieces']) == PARTS
            assert not errors, errors
            status.append('reduced motion, manual immediate mode and case replacement keep consistent source geometry')
            report.update(passed=True, browser=edge.version, original_part_count=len(model), page_errors=errors)
        except Exception as error:
            report.update(passed=False, error=str(error)[:2600], page_errors=errors)
            await page.screenshot(path=root/'explosion-failure.png')
            raise
        finally:
            (root/'explode-browser-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer('geometry-explode-browser', app='explode_browser_fixture:app', access_log=True)
    root = api.root
    print('GEOMETRY_EXPLODE_BROWSER_EVIDENCE', root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    (root/'explode_browser_fixture.py').write_text(
        'from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n", encoding='utf8')
    browser_tmp = root/'browser-tmp'
    browser_tmp.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        with api:
            case = api.create_case(CASE)
            workspace = api.call('GET', api.prefix)['workspace_id']
            api.call('POST', f"/workspaces/{workspace}/cases", {'name': EMPTY})
            saved = api.put('A-geo/geometry.py', SCRIPT)
            job = api.wait_job(saved['auto_preview_job'])
            assert job['exit_code'] == 0 and job['attempt']['process_stop']['verified']
            published = api.call('GET', api.prefix + '/geometry/scene')
            assert published['ocp_status'] == {'status': 'ready'}
            assert {node['name'] for node in published['objects']} == {'station', 'reference'}

            def leaves(node):
                if node.get('parts'):
                    return [item for child in node['parts'] for item in leaves(child)]
                return [node['name']]

            assert set(leaves(published['ocp']['data']['shapes'])) == PARTS
            before = hashlib.sha256(json.dumps(published, sort_keys=True).encode()).hexdigest()
            data = {'root': str(root), 'base': api.base, 'token': api.token, 'channel': args.channel}
            report.update(source_job=job['id'], publication=published['publication']['id'], native_leaves=sorted(PARTS))
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(data), text=True, check=True, timeout=300,
                env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
            assert hashlib.sha256(json.dumps(api.call('GET', api.prefix + '/geometry/scene'), sort_keys=True).encode()).hexdigest() == before
            api.create_case('Outside CAD explosion owner')
            api.call('GET', f"/cases/{case['id']}/geometry/scene", expect=404)
            report.update(scene_sha256=before, foreign_status=404)
        assert api.proc.poll() is not None
        report['passed'] = True
    finally:
        (root/'explode-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')


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
