"""Verify published MP4 playback, notes, actual speech tracks, and page order."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode


CASE = 'PPT narrated video acceptance'
SOURCE = 'E-report/video-source.pptx'
ENGLISH = 'E-report/build/english-narration.mp4'
CHINESE = 'E-report/build/chinese-narration.mp4'


def check_browser(config):
    from playwright.sync_api import expect, sync_playwright

    root = Path(config['root'])
    with sync_playwright() as play:
        browser = play.chromium.launch(channel='msedge')
        context = browser.new_context(viewport={'width': 1440, 'height': 920}, locale='zh-CN', accept_downloads=True)
        page = context.new_page()
        page.add_init_script('localStorage.setItem("vf_token",' + json.dumps(config['token']) + ');')
        page.goto(config['base'], wait_until='domcontentloaded')
        page.locator('[data-case-name=' + json.dumps(CASE) + ']').click(timeout=25000)
        page.locator('.dv-tab:visible').filter(has_text=re.compile('^caseDict$')).click()
        video = page.get_by_test_id('ppt-video-export')
        video.get_by_text('从 PPT 备注生成旁白视频', exact=True).click()
        video.get_by_label('选择 PPTX').select_option(SOURCE)
        video.get_by_label('视频输出路径').fill(ENGLISH)
        with page.expect_response(lambda response: response.url.endswith('/ppt:video') and response.request.method == 'POST') as submit:
            video.get_by_role('button', name='生成旁白视频', exact=True).click()
        assert submit.value.ok, submit.value.text()
        expect(video.get_by_role('status')).to_have_text('旁白视频已发布', timeout=90000)
        video.get_by_role('button', name='预览视频', exact=True).click()
        media = video.locator('video')
        expect(media).to_be_visible()
        playback = media.evaluate('''async video => {
            video.muted = true;
            await video.play();
            await new Promise(resolve => video.requestVideoFrameCallback(resolve));
            video.pause();
            return {duration: video.duration, width: video.videoWidth, height: video.videoHeight, time: video.currentTime};
        }''')
        assert playback['width'] == 1280 and playback['height'] == 720 and 3 < playback['duration'] < 30
        with page.expect_download() as download:
            video.get_by_role('button', name='下载 MP4', exact=True).click()
        download.value.save_as(root / 'english-narration.mp4')
        video.screenshot(path=root / 'narration-browser.png')
        (root / 'narration-browser.json').write_text(json.dumps({'playback': playback, 'job_id': submit.value.json()['job_id']},
                                                               indent=2) + '\n', encoding='utf-8')
        browser.close()


def native_evidence(root, file):
    from PIL import Image

    wsl = ['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec']
    path = subprocess.check_output(wsl + ['wslpath', '-a', str(file)], text=True).strip()
    destination = subprocess.check_output(wsl + ['wslpath', '-a', str(root)], text=True).strip()
    info = subprocess.check_output(wsl + ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path])
    info = json.loads(info)
    duration = float(info['format']['duration'])
    assert {stream['codec_name'] for stream in info['streams']} == {'h264', 'aac'}
    audio = subprocess.run(wsl + ['ffmpeg', '-hide_banner', '-i', path, '-vn', '-af', 'volumedetect', '-f', 'null', '-'],
                           capture_output=True, text=True, timeout=60, check=True)
    maximum = float(re.search(r'max_volume: (-?[0-9.]+) dB', audio.stderr)[1])
    assert -45 < maximum <= 0, maximum
    colors = []
    for index, timestamp in enumerate((0.5, duration - 0.5), 1):
        name = file.stem + f'-frame-{index}.png'
        subprocess.run(wsl + ['ffmpeg', '-v', 'error', '-ss', f'{timestamp:.3f}', '-i', path, '-frames:v', '1',
                              f'{destination}/{name}'], check=True, timeout=30)
        with Image.open(root / name) as frame:
            colors.append(frame.convert('RGB').getpixel((200, 640)))
    assert colors[0][2] > colors[0][0] and colors[1][0] > colors[1][2], colors
    return {'duration_seconds': duration, 'decoded_audio_max_db': maximum, 'first_and_last_slide_colors': colors,
            'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}


def main():
    import httpx
    from app.services.ppt_builder import build_pptx
    from testsupport import IsolatedServer

    dist = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    api = IsolatedServer('ff-e21-ppt-narration', app='ppt_fixture:app')
    (api.root / 'ppt_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf-8')
    spec = {'output': 'video-source.pptx', 'slides': [
        {'title': 'Thermal test preparation', 'notes': 'The controlled test is ready. 请核对仪器读数。',
         'native_shapes': [{'preset': 'rect', 'x': 0, 'y': 0.8, 'width': 1, 'height': 0.2, 'fill': '#176BA2'}]},
        {'title': 'Measured performance', 'notes': 'The peak temperature is within the limit. 温度结果符合要求。',
         'native_shapes': [{'preset': 'rect', 'x': 0, 'y': 0.8, 'width': 1, 'height': 0.2, 'fill': '#A23421'}]},
    ]}
    build_pptx(api.root, spec)
    original = (api.root / 'video-source.pptx').read_bytes()
    with api:
        api.create_case(CASE)
        response = httpx.post(api.base + '/api/v1' + api.prefix + '/upload', params={'path': SOURCE},
                              files={'file': ('video-source.pptx', original)},
                              headers={'Authorization': 'Bearer ' + api.token}, trust_env=False)
        response.raise_for_status()
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], '-X', 'utf8', __file__, '--edge'], check=True,
                       timeout=180, text=True, input=json.dumps({'base': api.base, 'token': api.token, 'root': str(api.root)}))
        ui = json.loads((api.root / 'narration-browser.json').read_text(encoding='utf-8'))
        english_status = api.wait_job(ui['job_id'])
        route = api.prefix + '/ppt:video'
        api.call('POST', route, {'input': SOURCE, 'output': ENGLISH}, expect=409)
        chinese = api.call('POST', route, {'input': SOURCE, 'output': CHINESE, 'language': 'chinese'}, expect=(200, 202))
        chinese_status = api.wait_job(chinese['job_id'])
        assert all(result['attempt']['process_stop']['verified'] for result in (english_status, chinese_status))
        for path in (ENGLISH, CHINESE):
            data = api.call('GET', api.prefix + '/raw?' + urlencode({'path': path}), raw=True)
            local = api.root / Path(path).name
            if local.is_file():
                assert local.read_bytes() == data
            else:
                local.write_bytes(data)
        assert api.call('GET', api.prefix + '/raw?' + urlencode({'path': SOURCE}), raw=True) == original
    report = {voice: native_evidence(api.root, api.root / (voice + '-narration.mp4')) for voice in ('english', 'chinese')}
    assert report['english']['sha256'] != report['chinese']['sha256']
    (api.root / 'ppt-narration.json').write_text(json.dumps({'passed': True, 'videos': report, 'browser': ui,
        'verified_exit': True, 'actual_llm_calls': 0, 'simulated_llm_calls': 0}, indent=2) + '\n', encoding='utf-8')
    print('PPT_NARRATION', api.root / 'ppt-narration.json', flush=True)


if __name__ == '__main__':
    check_browser(json.load(sys.stdin)) if sys.argv[1:] == ['--edge'] else main()
