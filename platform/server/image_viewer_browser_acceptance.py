"""Real authenticated file uploads and image viewing in Edge."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import zlib

import httpx


CASE = 'Actual image viewer'
LARGE = 'Ref/wide.png'
IMAGE = 'Ref/dimensions.png'
BROKEN = 'Ref/retry.png'
SLOW = 'Ref/slow.png'

APP = r'''
import asyncio, json, os, time
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from app.main import app
from app.services.process_identity_native import observe

root = Path(__file__).parent
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())))

@app.middleware('http')
async def product_conditions(request, call_next):
    if request.url.path.endswith('/raw') and request.query_params.get('path') == SLOW:
        (root/'hold-read').touch()
        deadline = time.monotonic() + 30
        while not (root/'release-read').exists() and time.monotonic() < deadline:
            await asyncio.sleep(.04)
    response = await call_next(request)
    if request.url.path == '/' and request.query_params.get('fullscreen') == 'denied':
        response.headers['Permissions-Policy'] = 'fullscreen=()'
    return response

app.mount('/', StaticFiles(directory=DIST, html=True))
'''


def png(width, height):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind+body))

    stripe = bytes([40, 86, 115]) * (width // 2) + bytes([158, 194, 163]) * (width - width // 2)
    inverse = bytes([174, 85, 64]) * (width // 3) + bytes([35, 47, 61]) * (width - width // 3)
    rows = (b'\0'+stripe) * (height // 3) + (b'\0'+inverse) * (height - height // 3)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))


def upload(base, prefix, token, path, payload, previous=None):
    conditions = {'expected_absent':'true'} if previous is None else {'expected_sha256':previous}
    response = httpx.post(base+'/api/v1'+prefix+'/upload', params={'path':path,**conditions},
        headers={'Authorization':'Bearer '+token}, files={'file':(Path(path).name,payload,'image/png')},
        timeout=30,trust_env=False)
    assert response.status_code==200, (response.status_code,response.text[:350])
    assert response.json()['content_sha256']==hashlib.sha256(payload).hexdigest()


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    result = {'passed':False,'stage':settings['stage']}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings['channel'])
        context = edge.new_context(viewport={'width':1480,'height':830},locale='zh-CN')
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(25000)
        expect.set_options(timeout=15000)
        viewer = page.get_by_test_id('image-viewer')
        canvas = page.get_by_test_id('image-canvas')
        image = page.get_by_test_id('image-img')
        error = page.get_by_test_id('image-error')
        scale = page.get_by_test_id('image-scale')
        reset = page.get_by_test_id('image-reset')
        rotate = page.get_by_test_id('image-rotate')
        fullscreen = page.get_by_test_id('image-fullscreen')
        console = []
        page.on('console',lambda message: console.append(message.text))

        def open_case(query=''):
            page.goto(base+'/'+query)
            page.locator('[data-case-name='+json.dumps(CASE)+']').click()
            folder = page.locator('[data-path="Ref"]')
            expect(folder).to_be_visible()
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()

        def open_file(path):
            page.locator('[data-path='+json.dumps(path)+']').click()
            expect(viewer).to_have_attribute('data-file',path)

        def dimensions(width,height):
            page.wait_for_function("""([width,height]) => {
              const img = document.querySelector('[data-testid=image-img]');
              return img?.complete && img.naturalWidth===width && img.naturalHeight===height;
            }""",arg=[width,height])
            box = image.bounding_box()
            assert box and box['width']>0 and box['height']>0
            return box

        def within(box,frame):
            return (box['x']>=frame['x']-1 and box['y']>=frame['y']-1
                    and box['x']+box['width']<=frame['x']+frame['width']+1
                    and box['y']+box['height']<=frame['y']+frame['height']+1)

        def raw(request,path):
            url = urlsplit(request.url)
            return url.path.endswith('/raw') and parse_qs(url.query).get('path')==[path]

        try:
            open_case()
            open_file(LARGE)
            dimensions(1920,210)
            rotate.click()
            wide_rotated = dimensions(1920,210)
            frame = canvas.bounding_box()
            assert wide_rotated['height']>wide_rotated['width']
            result['rotation'] = {'box':wide_rotated,'frame':frame,'fits':within(wide_rotated,frame)}
            page.screenshot(path=str(root/'image-rotated.png'))

            if settings['stage']=='baseline':
                assert not result['rotation']['fits']
                open_file(BROKEN)
                page.wait_for_function("""() => {
                  const img = document.querySelector('[data-testid=image-img]');
                  return img?.complete && img.naturalWidth===0;
                }""")
                expect(error).to_have_count(0)
                result.update(passed=True,decode_error_has_no_feedback=True)
                page.screenshot(path=str(root/'image-decode-baseline.png'))
                return

            assert result['rotation']['fits']
            open_file(IMAGE)
            initial = dimensions(240,120)
            expect(scale).to_have_text('100%')
            page.get_by_test_id('image-zoom-in').click()
            expect(scale).to_have_text('125%')
            assert abs(dimensions(240,120)['width']/initial['width']-1.25)<.01
            page.get_by_test_id('image-zoom-out').click()
            expect(scale).to_have_text('100%')
            center = (initial['x']+initial['width']/2,initial['y']+initial['height']/2)
            page.mouse.move(*center)
            page.mouse.wheel(0,-125)
            expect(scale).to_have_text('125%')
            before = dimensions(240,120)
            page.mouse.down()
            page.mouse.move(center[0]+65,center[1]+32,steps=7)
            page.mouse.up()
            moved = dimensions(240,120)
            assert abs(moved['x']-before['x']-65)<2 and abs(moved['y']-before['y']-32)<2
            page.mouse.dblclick(center[0],center[1])
            expect(scale).to_have_text('100%')
            assert abs(dimensions(240,120)['x']-initial['x'])<1
            assert not any('passive event listener' in message for message in console)
            result['actual_zoom_and_pointer'] = True

            fullscreen.click()
            expect(viewer).to_have_attribute('data-fullscreen','true')
            assert page.evaluate("document.fullscreenElement?.dataset.testid === 'image-viewer'")
            page.screenshot(path=str(root/'image-native-fullscreen.png'))
            fullscreen.click()
            expect(viewer).to_have_attribute('data-fullscreen','false')
            assert page.evaluate('document.fullscreenElement === null')
            open_case('?fullscreen=denied')
            open_file(IMAGE)
            dimensions(240,120)
            assert not page.evaluate('document.fullscreenEnabled')
            fullscreen.click()
            expect(viewer).to_have_attribute('data-fullscreen','true')
            box = viewer.bounding_box()
            viewport = page.viewport_size
            assert page.evaluate('document.fullscreenElement === null')
            assert abs(box['x'])<=1 and abs(box['y'])<=1, box
            assert abs(box['width']-viewport['width'])<=1 and abs(box['height']-viewport['height'])<=2, (box,viewport)
            page.screenshot(path=str(root/'image-fallback-fullscreen.png'))
            image_center = dimensions(240,120)
            page.mouse.move(image_center['x']+image_center['width']/2,image_center['y']+image_center['height']/2)
            page.mouse.wheel(0,-125)
            expect(scale).to_have_text('125%')
            page.keyboard.press('Escape')
            expect(viewer).to_have_attribute('data-fullscreen','false')
            expect(scale).to_have_text('125%')
            result['fallback_fullscreen_box'] = box
            result['both_fullscreen_paths'] = True

            with page.expect_response(lambda r: raw(r.request,BROKEN)) as broken:
                open_file(BROKEN)
            assert broken.value.status==200
            expect(error).to_contain_text('解码失败')
            page.screenshot(path=str(root/'image-decode-error.png'))
            upload(base,settings['prefix'],settings['token'],BROKEN,png(420,160),settings['broken_sha'])
            page.get_by_role('button',name='重新读取',exact=True).click()
            dimensions(420,160)
            expect(error).to_have_count(0)
            result['decoded_after_conditional_replacement_and_retry'] = True

            with page.expect_event('requestfailed',predicate=lambda r: raw(r,SLOW)) as aborted:
                open_file(SLOW)
                expect(viewer).to_contain_text('加载中')
                deadline = time.monotonic()+10
                while not (root/'hold-read').exists() and time.monotonic()<deadline:
                    page.wait_for_timeout(50)
                assert (root/'hold-read').exists()
                open_file(IMAGE)
            assert aborted.value.failure=='net::ERR_ABORTED'
            (root/'release-read').touch()
            dimensions(240,120)
            expect(error).to_have_count(0)
            reset.click()
            result['file_switch_aborts_old_read'] = True
            result['passed'] = True
        finally:
            (root/'release-read').touch()
            (root/'browser-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
            if not result['passed']:
                page.screenshot(path=str(root/'image-failure.png'))
            edge.close()


def verify(args):
    from app.services.process_identity_native import probe
    from testsupport import IsolatedServer

    api = IsolatedServer('image-viewer-browser',app='image_fixture:app',access_log=True)
    root = api.root
    print('IMAGE_VIEWER_EVIDENCE',root,flush=True)
    (root/'image_fixture.py').write_text(f'DIST = {str(args.dist.resolve(strict=True))!r}\nSLOW = {SLOW!r}\n'+APP,encoding='utf8')
    temp = root/'browser-temp'
    temp.mkdir()
    result = {'passed':False,'model_calls':0,'mock_llm_calls':0,'baseline':args.baseline}
    bad = b'not a valid encoded image'
    try:
        with api:
            api.create_case(CASE)
            prefix = api.prefix
            for path,data in [(LARGE,png(1920,210)),(IMAGE,png(240,120)),(BROKEN,bad),(SLOW,png(800,450))]:
                upload(api.base,prefix,api.token,path,data)
            settings = {'root':str(root),'base':api.base,'prefix':prefix,'token':api.token,'channel':args.channel,
                        'broken_sha':hashlib.sha256(bad).hexdigest(),'stage':'baseline' if args.baseline else 'fixed'}
            subprocess.run([str(args.browser_python.resolve(strict=True)),'-B','-X','utf8',str(Path(__file__)),'--browser'],
                input=json.dumps(settings),text=True,encoding='utf8',check=True,timeout=200,
                env=dict(os.environ,TMP=str(temp),TEMP=str(temp)))
            result['browser'] = json.loads((root/'browser-result.json').read_bytes())
            assert result['browser']['passed']
            birth = json.loads((root/'api-birth.json').read_bytes())
            api.create_case('Outside image owner')
            api.call('GET',prefix+'/raw?'+urlencode({'path':IMAGE}),expect=404,raw=True)
            api.token = None
            api.call('GET',prefix+'/raw?'+urlencode({'path':IMAGE}),expect=401,raw=True)
            result['foreign_and_anon'] = [404,401]
        deadline = time.monotonic()+12
        while probe(birth)['state']!='exited' and time.monotonic()<deadline:
            time.sleep(.07)
        assert probe(birth)['state']=='exited'
        with socket.socket() as connection:
            connection.settimeout(.3)
            assert connection.connect_ex(('127.0.0.1',api.port)) != 0
        result['api_exited'] = result['passed'] = True
    finally:
        (root/'image-viewer-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print('IMAGE_VIEWER_PASS',root,flush=True)


if __name__ == '__main__':
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from regression_llm_policy import enforce
        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        parser.add_argument('--channel',default='msedge')
        parser.add_argument('--baseline',action='store_true')
        verify(parser.parse_args())
