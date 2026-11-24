"""Exercise all eight CAD tracks against a real nested OCP assembly in Edge."""

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

from geometry_explode_browser_acceptance import PARTS, SCRIPT


CASE = "CAD keyframe targets"
OTHER = "Duplicate assembly in a separate case"
SCHEMA = "vibeflow.cadquery.animation.v1"

VIEW = """() => {
  const host = document.querySelector('[data-testid=geo-canvas-host]');
  let fiber = host[Object.keys(host).find(key => key.startsWith('__reactFiber$'))];
  let scene;
  while (fiber && !scene) {
    if (typeof fiber.type === 'function') {
      for (let hook = fiber.memoizedState; hook; hook = hook.next) {
        if (hook.memoizedState?.current?.isScene) scene = hook.memoizedState.current;
      }
    }
    fiber = fiber.return;
  }
  if (!scene) throw new Error('Mounted CAD scene missing');
  const artifacts = scene.children.filter(child => child.userData.vfObjectKey).map(child => ({
    name: child.userData.vfMesh, key: child.userData.vfObjectKey, cap: !!child.userData.clipCap,
    kind: child.isMesh ? 'mesh' : 'edge', position: child.position.toArray(),
    quaternion: child.quaternion.toArray(), center: child.userData.center.toArray(),
    bbox: child.userData.bbox, geometry: child.geometry.uuid,
  }));
  return {artifacts, time: Number(host.dataset.trackTime), playing: host.dataset.trackPlaying === 'true'};
}"""


def multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [
        ax*bw + aw*bx + ay*bz - az*by,
        ay*bw + aw*by + az*bx - ax*bz,
        az*bw + aw*bz + ax*by - ay*bx,
        aw*bw - ax*bx - ay*by - az*bz,
    ]


def axis_rotation(axis, degrees):
    half = math.radians(degrees) / 2
    xyz = [0, 0, 0]
    xyz[axis] = math.sin(half)
    return xyz + [math.cos(half)]


def equivalent_rotation(actual, expected):
    return math.isclose(abs(sum(a*b for a, b in zip(actual, expected, strict=True))), 1.0, abs_tol=1e-8)


def compare_pose(state, originals, time):
    expected = axis_rotation(2, 90*time)
    for axis, degrees in enumerate((120, 90, -60)):
        expected = multiply(expected, axis_rotation(axis, degrees*time))
    rotor_count = 0
    for artifact in state['artifacts']:
        name = artifact['name']
        base = originals[name]
        assert artifact['key'] == base['key']
        path = [6*time, 2*time, 18*time] if name == 'rotor' else ([0, 4*time, 0] if name == 'shaft' else [0, 0, 0])
        position = [actual+shift for actual, shift in zip(base['position'], path, strict=True)]
        assert all(abs(a-b)<1e-7 for a,b in zip(artifact['position'], position, strict=True)), (artifact, position)
        target = multiply(base['quaternion'], expected) if name == 'rotor' else base['quaternion']
        assert equivalent_rotation(artifact['quaternion'], target), (artifact, target)
        rotor_count += int(name == 'rotor')
    assert rotor_count >= 2


def make_document(inserted):
    key = inserted['objectId']
    same_part = [
        ('t', [[0, 0, 0], [4, 6, 8]]), ('tx', [0, 2]), ('ty', [0, -4]), ('tz', [0, 10]),
        ('q', [[0, 0, 0, 1], axis_rotation(2, 90)]), ('rx', [0, 120]), ('ry', [0, 90]), ('rz', [0, -60]),
    ]
    tracks = [{'selector': 'bearing', 'objectId': key, 'action': channel, 'times': [0, 1], 'values': values}
              for channel, values in same_part]
    tracks.append({'selector': '/shaft', 'action': 'ty', 'times': [0, 1], 'values': [0, 4]})
    return {'schema': SCHEMA, 'speed': 1, 'repeat': 'false', 'tracks': tracks}


async def browser(data):
    from playwright.async_api import async_playwright, expect

    root, base = Path(data['root']), data['base']
    report, errors = {'checks': []}, []
    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=data['channel'], args=['--enable-unsafe-swiftshader'])
        context = await edge.new_context(viewport={'width': 1536, 'height': 1120},
            locale='zh-CN', accept_downloads=True)
        await context.add_init_script(
            'if (location.origin === ' + json.dumps(base) + ') localStorage.setItem("vf_token", '
            + json.dumps(data['token']) + ');')
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_default_timeout(30000)
        host = page.get_by_test_id('geo-canvas-host')
        tree = page.get_by_test_id('cad-tree')
        editor = tree.get_by_role('textbox', name='关键帧 JSON', exact=True)
        apply = tree.get_by_test_id('cad-track-apply')
        play = tree.get_by_test_id('cad-track-play')
        pause = tree.get_by_test_id('cad-track-pause')
        stop = tree.get_by_test_id('cad-track-stop')
        scrub = tree.get_by_role('slider', name='轨道时间', exact=True)
        clip_axis = tree.get_by_role('combobox', name='剖切轴', exact=True)
        clip_slider = tree.get_by_role('slider', name='切面位置', exact=True)
        status = tree.get_by_test_id('cad-track-status')
        checks = report['checks']

        async def state():
            return await page.evaluate(VIEW)

        async def activate(case):
            row = page.locator('[data-case-name=' + json.dumps(case) + ']')
            await row.click()
            await expect(row).to_have_attribute('aria-pressed', 'true')
            if not await host.is_visible():
                await page.locator('.dv-tab:visible').filter(has_text=re.compile('^几何预览$')).click()
            if not await tree.is_visible():
                await page.get_by_role('button', name='对象与设置', exact=True).click()
            mode = tree.get_by_role('combobox', name='对象树视图')
            await expect(mode).to_be_visible()
            if await mode.input_value() != 'assembly':
                await mode.select_option('assembly')
            await expect(tree.get_by_role('button', name='rotor', exact=True)).to_be_visible()

        async def set_slider(control, number):
            await control.evaluate("""(input, value) => {
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
            }""", number)

        async def load(document):
            await editor.fill(json.dumps(document, indent=2))
            await apply.click()

        async def fit_capture(path):
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            return await host.locator('canvas').screenshot(path=path)

        async def await_time(predicate, seconds=5):
            async with asyncio.timeout(seconds):
                while True:
                    observed = await state()
                    if predicate(observed):
                        return observed
                    await asyncio.sleep(.025)

        try:
            await page.goto(base, wait_until='domcontentloaded')
            await activate(CASE)
            await expect(play).to_be_disabled()
            originals = {item['name']: item for item in (await state())['artifacts'] if item['kind'] == 'mesh'}
            assert set(originals) == PARTS and len({item['key'] for item in originals.values()}) == len(PARTS)
            await tree.get_by_role('button', name='rotor', exact=True).click()
            source = await fit_capture(root/'tracks-source.png')
            properties = tree.get_by_test_id('cad-properties')
            await properties.get_by_role('combobox', name='轨道动作', exact=True).select_option('t')
            await properties.get_by_role('button', name='添加对象轨道', exact=True).click()
            await expect(editor).to_be_focused()
            staged, = json.loads(await editor.input_value())['tracks']
            assert staged['selector'] == 'rotor' and staged['action'] == 't'
            assert staged['objectId'] == originals['rotor']['key'] and staged['objectId'].startswith('path:')
            checks.append('selected OCP instance creates an editable track with a stable ID')

            document = make_document(staged)
            await load(document)
            await expect(status).to_contain_text('已应用 9 条轨道')
            await expect(properties.get_by_test_id('cad-object-tracks')).to_contain_text('四元数旋转')
            await expect(scrub).to_be_enabled()
            await set_slider(scrub, .5)
            await expect(scrub).to_have_attribute('aria-valuetext', '0.50 秒')
            middle = await state()
            compare_pose(middle, originals, .5)
            report['exact_midpoint'] = {item['name']: {'offset': item['position'], 'quaternion': item['quaternion']}
                for item in middle['artifacts'] if item['kind']=='mesh' and item['name'] in {'rotor', 'shaft', 'bearing'}}
            await fit_capture(root/'tracks-eight-channels.png')
            await properties.get_by_test_id('cad-object-tracks').scroll_into_view_if_needed()
            await page.screenshot(path=root/'tracks-object-link.png')
            checks.append('all eight ordered channels and independent legacy target match numerical poses')

            await play.click()
            await await_time(lambda observed: observed['playing'] and observed['time'] > .53)
            await pause.click()
            await expect(host).to_have_attribute('data-track-playing', 'false')
            await set_slider(scrub, .5)
            await expect(host).to_have_attribute('data-track-time', '0.500')
            high = max(item['bbox']['max'][2] for item in originals.values())
            low = min(item['bbox']['min'][2] for item in originals.values())
            clip_position = round((originals['rotor']['center'][2]-low)/(high-low), 2)
            await clip_axis.select_option('z')
            await set_slider(clip_slider, clip_position)
            await expect(clip_slider).to_have_value(f'{clip_position:g}')
            clipped = await await_time(lambda observed: any(item['cap'] and item['name']=='rotor'
                for item in observed['artifacts']))
            compare_pose(clipped, originals, .5)
            await asyncio.sleep(.25)
            held = await state()
            assert held == clipped
            report['paused_cap_pieces'] = [item['kind'] for item in held['artifacts']
                if item['name']=='rotor' and item['cap']]
            await page.screenshot(path=root/'tracks-paused-section.png')
            await clip_axis.select_option('')
            await expect(host).to_have_attribute('data-clip-caps','0')
            compare_pose(await state(), originals, .5)
            checks.append('paused/scrubbed pose survives cap/edge reconstruction and source restore')

            for bad, expected in (
                ({**document, 'schema': 'vibeflow.animation.camera.v1'}, '可视化相机轨道'),
                ({**document, 'repeat': 'maybe'}, 'repeat 须为布尔值'),
                ({**document, 'tracks': [{**document['tracks'][0], 'objectId': 'path:/missing-instance'}]},
                 '找不到轨道实例'),
            ):
                await load(bad)
                await expect(status).to_contain_text(expected)
                await expect(host).to_have_attribute('data-track-time', '0.500')
                compare_pose(await state(), originals, .5)
            async with page.expect_download() as downloading:
                await tree.get_by_test_id('cad-track-export').click()
            download = await downloading.value
            assert download.suggested_filename == 'cad-animation.json'
            exported = root/'applied-tracks.json'
            await download.save_as(exported)
            decoded = json.loads(exported.read_text(encoding='utf8'))
            assert decoded['schema'] == SCHEMA and decoded['repeat'] is False
            assert all(item['objectId'] == staged['objectId'] for item in decoded['tracks'][:8])
            assert 'objectId' not in decoded['tracks'][8]
            checks.append('bad targets, repeat and camera JSON preserve applied pose and downloadable v1 document')

            await play.click()
            await expect(host).to_have_attribute('data-track-playing','true')
            await expect(status).to_contain_text('轨道播放完成')
            await expect(host).to_have_attribute('data-track-time','0.000')
            compare_pose(await state(), originals, 0)
            await load({**decoded, 'repeat': True})
            await expect(status).to_contain_text('已应用 9 条轨道')
            await set_slider(scrub, .90)
            await play.click()
            wrapped = await await_time(lambda v: v['playing'] and .03 < v['time'] < .7)
            assert wrapped['playing']
            await pause.click()
            await expect(host).to_have_attribute('data-track-playing','false')
            await expect(pause).to_be_disabled()
            await stop.click()
            compare_pose(await state(), originals, 0)
            assert await fit_capture(root/'tracks-restored.png') == source
            checks.append('finite playback restores source, repeat wraps, stop restores exact canvas')

            await set_slider(scrub, .50)
            await expect(host).to_have_attribute('data-track-time','0.500')
            draft = await editor.input_value()
            await page.get_by_role('button', name='重载场景', exact=True).click()
            await expect(status).to_contain_text('重新应用轨道')
            await expect(play).to_be_disabled()
            await expect(scrub).to_be_disabled()
            assert await editor.input_value() == draft
            await apply.click()
            await expect(status).to_contain_text('已应用 9 条轨道')
            await set_slider(scrub, .5)
            fresh = {item['name']: item for item in (await state())['artifacts'] if item['kind']=='mesh'}
            assert {name: item['key'] for name, item in fresh.items()} == {
                name: item['key'] for name, item in originals.items()}
            compare_pose(await state(), originals, .5)
            await activate(OTHER)
            await expect(play).to_be_disabled()
            await expect(scrub).to_be_disabled()
            assert staged['objectId'] not in await editor.input_value()
            compare_pose(await state(), originals, 0)
            await activate(CASE)
            await expect(play).to_be_disabled()
            await expect(host).to_have_attribute('data-track-count', '0')
            compare_pose(await state(), originals, 0)
            assert not errors, errors
            checks.append('same-case scene replacement requires explicit reapply; identical second case cannot reuse draft or binding')
            report.update(passed=True, browser=edge.version, page_errors=errors, object_count=len(originals))
        except Exception as error:
            report.update(passed=False, error=str(error)[:2600], page_errors=errors)
            await page.screenshot(path=root/'tracks-failure.png')
            raise
        finally:
            (root/'keyframes-browser-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer('geometry-keyframes-browser', app='keyframes_browser_fixture:app', access_log=True)
    root = api.root
    print('GEOMETRY_KEYFRAMES_BROWSER_EVIDENCE', root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    (root/'keyframes_browser_fixture.py').write_text(
        'from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n", encoding='utf8')
    browser_tmp = root/'browser-tmp'
    browser_tmp.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        primitive = subprocess.run(['node', str(Path(__file__).resolve().parents[1] / 'tools/cad-animation-smoke.mjs')],
            capture_output=True, text=True, encoding='utf8', timeout=30)
        (root/'keyframe-primitives.log').write_text(primitive.stdout + primitive.stderr, encoding='utf8')
        primitive.check_returncode()
        with api:
            first = api.create_case(CASE)
            workspace = api.call('GET', api.prefix)['workspace_id']
            second = api.call('POST', f"/workspaces/{workspace}/cases", {'name': OTHER})
            originals = {}
            for case in (first, second):
                route = f"/cases/{case['id']}"
                saved = api.call('PUT', route+'/file', {'path':'A-geo/geometry.py', 'content': SCRIPT})
                job = api.wait_job(saved['auto_preview_job'])
                assert job['exit_code']==0 and job['attempt']['process_stop']['verified']
                record = api.call('GET', route+'/geometry/scene')
                assert record['ocp_status']=={'status':'ready'}
                originals[route] = {'job':job['id'], 'publication':record['publication']['id'],
                    'sha256':hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()}
            report['native_scenes'] = originals
            browser_data = {'root':str(root), 'base':api.base, 'token':api.token, 'channel':args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(browser_data), text=True, check=True, timeout=300,
                env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
            for route, old in originals.items():
                new = api.call('GET', route+'/geometry/scene')
                assert hashlib.sha256(json.dumps(new, sort_keys=True).encode()).hexdigest()==old['sha256']
            api.create_case('Outside CAD track owner')
            api.call('GET', f"/cases/{first['id']}/geometry/scene", expect=404)
            report['foreign_status'] = 404
        assert api.proc.poll() is not None
        report['passed'] = True
    finally:
        (root/'keyframes-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')


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
