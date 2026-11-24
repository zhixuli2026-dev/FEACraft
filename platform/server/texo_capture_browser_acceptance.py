"""Exercise native capture of an isolated tab and a formula on a Windows display."""
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


SOURCE_TITLE = 'VibeFlow isolated formula source 8be7'


def open_panel(context, config):
    from playwright.sync_api import expect

    context.add_init_script('if (location.origin === ' + json.dumps(config['base'])
                            + ') localStorage.setItem("vf_token", ' + json.dumps(config['token']) + ');')
    page = context.new_page()
    page.goto(config['base'], wait_until='domcontentloaded')
    page.locator('[data-case-name=' + json.dumps(config['case_name']) + ']').click(timeout=25000)
    page.locator('.dv-tab').get_by_text('公式识别', exact=True).click(timeout=25000)
    worker = page.locator('iframe[title="texo-worker"]')
    expect(worker).to_be_attached()
    worker.element_handle().content_frame().wait_for_function("typeof window.vfTexoRecognize === 'function'")
    return page, worker.locator('..')


def share(page, panel):
    from playwright.sync_api import expect

    page.bring_to_front()
    panel.get_by_test_id('screen-capture').click()
    expect(panel.get_by_test_id('texo-ocr-status')).to_contain_text('请拖动框选', timeout=20000)
    video = panel.locator('video')
    return video.evaluate('''video => {
        const track = video.srcObject.getVideoTracks()[0];
        window.captureAcceptanceTrack = track;
        return {width: video.videoWidth, height: video.videoHeight, state: track.readyState,
                settings: track.getSettings(), audioTracks: video.srcObject.getAudioTracks().length};
    }''')


def keystroke_to_browser(browser_pid):
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    prior = user32.GetForegroundWindow()
    actual = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(prior, ctypes.byref(actual))
    assert actual.value == browser_pid, 'The isolated browser must own the foreground picker'
    user32.keybd_event(0x1b, 0, 0, 0)
    user32.keybd_event(0x1b, 0, 2, 0)


def exercise_monitor(play, config, formula, proof):
    import tkinter

    from playwright.sync_api import expect
    from texo_accuracy_browser_acceptance import tokens

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # Per monitor v2, coordinates in native pixels.
    count = user32.GetSystemMetrics(80)  # SM_CMONITORS
    proof['physical_monitors_sampled'] = count
    if count != 1:
        # The automatic selector cannot choose a known display when multiple are attached.
        proof['native_monitor'] = 'single_display_fixture_required'
        return
    source = tkinter.Tk()
    source.overrideredirect(True)
    source.attributes('-topmost', True)
    background = '#2a746d'
    source.configure(background=background)
    image = tkinter.PhotoImage(data=formula).zoom(2)
    label = tkinter.Label(source, image=image, borderwidth=0, highlightthickness=0)
    padding = 14
    label.pack(padx=padding, pady=padding)
    screen_width = user32.GetSystemMetrics(0)
    screen_height = user32.GetSystemMetrics(1)
    source.geometry(f'+{screen_width - image.width() - padding * 2 - 48}+70')
    source.update()
    browser = None
    try:
        source_x, source_y = label.winfo_rootx(), label.winfo_rooty()
        browser = play.chromium.launch(channel='msedge', headless=False, args=[
            '--window-size=1536,930', '--force-device-scale-factor=1.25', '--auto-select-screen-capture-source'])
        page, panel = open_panel(browser.new_context(no_viewport=True, locale='zh-CN'), config)
        source.update()
        track = share(page, panel)
        proof['monitor_track'] = track
        assert track['state'] == 'live' and track['settings']['displaySurface'] == 'monitor', track
        scale_x, scale_y = track['width'] / screen_width, track['height'] / screen_height
        assert track['audioTracks'] == 0 and abs(scale_x - scale_y) < .01, track
        proof['physical_monitor_size'] = {'width': screen_width, 'height': screen_height}
        proof['monitor_capture_scale'] = {'x': scale_x, 'y': scale_y}
        # Confirm the visible source marker from the real video before cropping. Save only the formula.
        page.wait_for_function('''([x, y]) => {
            const video = document.querySelector('video');
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = 1;
            const context = canvas.getContext('2d');
            context.drawImage(video, x, y, 1, 1, 0, 0, 1, 1);
            const [r,g,b] = context.getImageData(0,0,1,1).data;
            return Math.abs(r-42) + Math.abs(g-116) + Math.abs(b-109) < 18;
        }''', arg=[round((source_x - padding // 2) * scale_x), round((source_y - padding // 2) * scale_y)], timeout=8000)
        box = panel.get_by_test_id('screen-crop-area').bounding_box()
        assert box
        # Leave one native pixel of tolerance at each edge of the formula render.
        to_x = lambda x: box['x'] + box['width'] * x / screen_width
        to_y = lambda y: box['y'] + box['height'] * y / screen_height
        page.mouse.move(to_x(source_x + 1), to_y(source_y + 1))
        page.mouse.down()
        page.mouse.move(to_x(source_x + image.width() - 1), to_y(source_y + image.height() - 1), steps=10)
        page.mouse.up()
        expect(panel.get_by_test_id('screen-crop-selection')).to_be_visible()
        panel.get_by_test_id('screen-crop-apply').click()
        page.wait_for_function('window.captureAcceptanceTrack.readyState === "ended"')
        preview = panel.get_by_alt_text('待识别', exact=True)
        dimensions = preview.evaluate('''async image => {
            await image.decode();
            return {width:image.naturalWidth, height:image.naturalHeight};
        }''')
        assert abs(dimensions['width'] - image.width() * scale_x) <= 5
        assert abs(dimensions['height'] - image.height() * scale_y) <= 5
        panel.get_by_role('button', name='识别公式', exact=True).click()
        expect(panel.get_by_test_id('texo-ocr-status')).to_have_text('识别完成', timeout=120000)
        latex = panel.get_by_role('textbox', name='识别结果').input_value()
        assert tokens(latex) == tokens(r'E=mc^{2}'), latex
        cropped = base64.b64decode(preview.get_attribute('src').split(',', 1)[1])
        (Path(config['root']) / 'cropped-monitor-formula.png').write_bytes(cropped)
        proof['actual_formulanet_runs'] += 1
        proof.update(native_monitor=True, monitor_crop=dimensions, monitor_latex=latex, monitor_track_ended=True)
    finally:
        if browser is not None:
            browser.close()
        source.destroy()


def exercise(config):
    from playwright.sync_api import expect, sync_playwright
    from texo_accuracy_browser_acceptance import tokens

    root = Path(config['root'])
    proof = {'passed': False, 'native_tab': False, 'native_cancel': False,
             'physical_monitors_sampled': 0, 'actual_formulanet_runs': 0,
             'actual_llm_calls': 0, 'simulated_llm_calls': 0}
    formula = base64.b64encode(Path(config['sample']).read_bytes()).decode('ascii')
    args = ['--window-size=1536,930', '--force-device-scale-factor=1.25']
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(channel='msedge', headless=False, args=[*args,
                '--auto-select-tab-capture-source-by-title=' + SOURCE_TITLE])
            proof['browser'] = browser.version
            context = browser.new_context(no_viewport=True, locale='zh-CN')
            source = context.new_page()
            source.set_content(f'<title>{SOURCE_TITLE}</title><style>body{{margin:0;background:#417e7b}}'
                '#formula{{position:fixed;left:18vw;top:22vh;width:340px;height:auto;display:block}}</style>'
                f'<img id="formula" alt="E=mc squared" src="data:image/png;base64,{formula}">')
            coordinates = source.locator('#formula').evaluate('''async element => {
                await element.decode();
                const rect = element.getBoundingClientRect();
                return {left: rect.left / innerWidth, right: rect.right / innerWidth,
                        top: rect.top / innerHeight, bottom: rect.bottom / innerHeight, dpr: devicePixelRatio};
            }''')
            page, panel = open_panel(context, config)
            track = share(page, panel)
            assert track['state'] == 'live' and track['audioTracks'] == 0, track
            assert track['settings']['displaySurface'] == 'browser', track
            proof['native_tab'] = True
            proof['source_device_scale_factor'] = coordinates['dpr']
            proof['source_track'] = track
            box = panel.get_by_test_id('screen-crop-area').bounding_box()
            assert abs(track['width'] / track['height'] - box['width'] / box['height']) < 0.04, (track, box)
            assert track['width'] > box['width'] * 1.5, (track, box)
            # Reverse drag covers the formula on the live source at non-unit scaling.
            page.mouse.move(box['x'] + box['width'] * (coordinates['right'] - 0.004),
                            box['y'] + box['height'] * (coordinates['bottom'] - 0.008))
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] * (coordinates['left'] + 0.004),
                            box['y'] + box['height'] * (coordinates['top'] + 0.008), steps=10)
            page.mouse.up()
            expect(panel.get_by_test_id('screen-crop-selection')).to_be_visible()
            apply = panel.get_by_test_id('screen-crop-apply')
            apply.click()
            page.wait_for_function('window.captureAcceptanceTrack.readyState === "ended"')
            preview = panel.get_by_alt_text('待识别', exact=True)
            dimensions = preview.evaluate('''async image => {
                await image.decode();
                return {width: image.naturalWidth, height: image.naturalHeight};
            }''')
            expected_width = track['width'] * (coordinates['right'] - coordinates['left'] - 0.008)
            assert abs(dimensions['width'] - expected_width) <= 5, (dimensions, expected_width)
            (root / 'cropped-formula.png').write_bytes(base64.b64decode(preview.get_attribute('src').split(',', 1)[1]))
            panel.get_by_role('button', name='识别公式', exact=True).click()
            expect(panel.get_by_test_id('texo-ocr-status')).to_have_text('识别完成', timeout=120000)
            observed = panel.get_by_role('textbox', name='识别结果').input_value()
            assert tokens(observed) == tokens(r'E=mc^{2}'), observed
            proof.update(actual_formulanet_runs=1, latex=observed, crop=dimensions, apply_stopped_track=True)

            share(page, panel)
            panel.get_by_role('button', name='停止共享', exact=True).click()
            page.wait_for_function('window.captureAcceptanceTrack.readyState === "ended"')
            expect(panel.locator('video')).to_have_count(0)
            proof['explicit_stop_ended_track'] = True
            share(page, panel)
            source.close()
            page.wait_for_function('window.captureAcceptanceTrack.readyState === "ended"')
            expect(panel.get_by_test_id('texo-ocr-status')).to_have_text('屏幕共享已停止')
            proof['source_closed_ended_track'] = True
            browser.close()

            exercise_monitor(play, config, formula, proof)

            browser = play.chromium.launch(channel='msedge', headless=False, args=args)
            pid = next(p['id'] for p in browser.new_browser_cdp_session().send('SystemInfo.getProcessInfo')['processInfo']
                       if p['type'] == 'browser')
            page, panel = open_panel(browser.new_context(no_viewport=True, locale='zh-CN'), config)
            page.bring_to_front()
            toggle = panel.get_by_test_id('screen-capture')
            toggle.click()
            expect(toggle).to_be_disabled()
            time.sleep(0.8)  # Give Edge's native picker time to take keyboard focus.
            keystroke_to_browser(pid)
            expect(panel.get_by_test_id('texo-ocr-status')).to_have_text('屏幕共享已取消或未授权', timeout=12000)
            expect(toggle).to_be_enabled()
            expect(panel.locator('video')).to_have_count(0)
            proof['native_cancel'] = True
            proof['passed'] = True
            browser.close()
    finally:
        (root / 'texo-native-capture.json').write_text(json.dumps(proof, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def main(sample):
    from testsupport import IsolatedServer

    dist = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    api = IsolatedServer('ff-e20-texo-capture', app='texo_fixture:app')
    (api.root / 'texo_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf-8')
    with api:
        case_name = 'Isolated capture acceptance'
        api.create_case(case_name)
        settings = {'base': api.base, 'root': str(api.root), 'sample': str(Path(sample).resolve(strict=True)),
                    'case_name': case_name, 'token': api.token}
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], '-X', 'utf8', __file__, '--edge'],
                       input=json.dumps(settings), text=True, timeout=395, check=True)
    print('TEXO_CAPTURE', api.root / 'texo-native-capture.json', flush=True)


if __name__ == '__main__':
    exercise(json.load(sys.stdin)) if sys.argv[1:] == ['--edge'] else main(sys.argv[1])
