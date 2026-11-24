"""Send newly generated visual evidence through Edge, platform Agent, and official DeepSeek."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlencode, urlsplit

from live_llm_acceptance import FIXTURE, source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from testsupport import IsolatedServer


CASE = 'Actual image reading acceptance'


def create_picture(path):
    from PIL import Image, ImageDraw, ImageFont

    code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRTUVWXYZ23456789') for _ in range(6))
    fonts = Path(os.environ['WINDIR']) / 'Fonts'
    image = Image.new('RGB', (770, 280), '#fffef4')
    draw = ImageDraw.Draw(image)
    draw.text((45, 36), 'Equipment service identifier', fill='#17273f',
              font=ImageFont.truetype(str(fonts / 'consola.ttf'), 32))
    draw.text((45, 115), code, fill='#131b28', font=ImageFont.truetype(str(fonts / 'consolab.ttf'), 98))
    image.save(path)
    return code, image.size


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    base, root = settings['base'], Path(settings['root'])
    cid = settings['conversation']
    result = {'passed': False, 'turns': []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1500, 'height': 1000}, locale='zh-CN')
        context.add_init_script(f"if(location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        page = context.new_page()
        chooser = page.get_by_test_id('agent-conversation')
        composer = page.get_by_test_id('agent-input')

        def enter():
            case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(case).to_be_visible(timeout=30000)
            if case.get_attribute('aria-pressed') != 'true':
                case.click()
            if not chooser.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            chooser.select_option(str(cid))
            expect(composer).to_be_enabled()

        def send(prompt, image_count):
            composer.fill(prompt)
            endpoint = f'/api/v1/conversations/{cid}/messages'
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and urlsplit(response.url).path == endpoint, timeout=120000) as response:
                page.get_by_test_id('agent-send').click()
            answer = response.value
            assert answer.ok and len(answer.request.post_data_json['images']) == image_count
            frames = [json.loads(line[5:]) for line in answer.text().splitlines() if line.startswith('data:')]
            content = ''.join(frame.get('text', '') for frame in frames if frame['type'] == 'text.delta').strip()
            assert sum(frame['type'] == 'run.completed' for frame in frames) == 1, [frame['type'] for frame in frames]
            assert settings['code'] in content, content
            result['turns'].append({'images_in_current_request': image_count, 'answer': content})

        try:
            page.goto(base, wait_until='domcontentloaded')
            enter()
            upload = page.get_by_test_id('agent-image-input')
            upload.set_input_files([str(root / 'equipment.png'), str(root / 'discard.png')])
            expect(page.get_by_test_id('attachment-chip')).to_have_count(2)
            page.get_by_role('button', name='移除图片 discard.png', exact=True).click()
            expect(page.get_by_test_id('attachment-chip')).to_have_count(1)
            send('Read the equipment service identifier visibly printed in the attached image. Reply with the six letters or digits only.', 1)
            expect(page.get_by_test_id('attachment-chip')).to_have_count(0)
            page.reload(wait_until='domcontentloaded')
            enter()
            rendered = page.get_by_test_id('chat-image')
            expect(rendered).to_have_count(1)
            expect(rendered).to_have_attribute('src', re.compile(r'^blob:'))
            assert rendered.evaluate('(image) => image.decode().then(() => [image.naturalWidth, image.naturalHeight])') == settings['dimensions']
            send('Look again at the image from my immediately preceding message. Give its identifier only, read visually.', 0)
            expect(page.get_by_test_id('agent-send')).to_be_disabled()
            page.screenshot(path=root / 'visible-image-history.png')
            result.update(passed=True, browser=edge.version, refreshed_blob=True)
        finally:
            (root / 'browser.json').write_text(json.dumps(result, indent=2), encoding='utf8')
            edge.close()


def run(args, config):
    api = IsolatedServer('live-f67-agent-image', app='image_fixture:app')
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file() and config['model'] == 'deepseek-flash'
    fixture = 'live_request_limit=2\n' + FIXTURE + '''
import os
import hashlib,base64
from cryptography.fernet import Fernet
from app.services.process_identity_native import observe
vault = Fernet(Fernet.generate_key())
llm._vault = lambda: vault
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')
forward_send=httpx.AsyncClient.send
async def observe_image(self,request,*args,**kwargs):
 if request.url.host=='api.deepseek.com' and request.url.path.endswith('/chat/completions'):
  body=json.loads(request.content)
  digests=[]
  for message in body['messages']:
   if isinstance(message['content'],list):
    for part in message['content']:
     if part['type']=='image_url':
      value=part['image_url']['url']
      assert value.startswith('data:image/png;base64,')
      digests.append(hashlib.sha256(base64.b64decode(value.split(',',1)[1],validate=True)).hexdigest())
  receipt({'kind':'request_image_digests','sha256':digests})
 return await forward_send(self,request,*args,**kwargs)
httpx.AsyncClient.send=observe_image
'''
    fixture += f'\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n'
    (api.root / 'image_fixture.py').write_text(fixture, encoding='utf8')
    print('LIVE_F67_EVIDENCE', api.root, flush=True)
    picture = api.root / 'equipment.png'
    code, dimensions = create_picture(picture)
    create_picture(api.root / 'discard.png')
    content = picture.read_bytes()
    result = {'passed': False, 'provider': 'deepseek', 'model': config['model'], 'mock_llm_calls': 0}
    try:
        with api:
            case = api.create_case(CASE)
            profile = api.call('POST', '/provider-profiles', {'name': 'Authorized vision', 'provider': 'deepseek',
                 'base_url': config['base_url'], 'api_key': config['key'], 'default_model': config['model']})
            cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Live image history',
                 'provider_profile_id': profile['id'], 'model': config['model'], 'permission_mode': 'ask'})['id']
            endpoint = f'/conversations/{cid}/messages'
            valid = {'name': 'equipment.png', 'mime': 'image/png', 'data': base64.b64encode(content).decode('ascii')}
            for bad in ({**valid, 'data': 'data:image/png;base64'}, {**valid, 'mime': 'image/gif'}, {**valid, 'data': 'bad!'}):
                api.call('POST', endpoint, {'content': '', 'provider_profile_id': profile['id'],
                         'images': [valid, bad]}, expect=400)
            before = api.call('GET', f'/conversations/{cid}')
            assert not any(event['type'] == 'message' for event in before['events'])
            assert not list((api.root / 'files').rglob('equipment.png'))
            result['invalid_batches_rejected_without_writes'] = 3
            temp = api.root / 'edge-tmp'
            temp.mkdir()
            subprocess.run([str(args.browser_python), '-B', '-X', 'utf8', __file__, '--browser'], cwd=api.root,
                input=json.dumps({'root': str(api.root), 'base': api.base, 'token': api.token, 'conversation': cid,
                                  'dimensions': dimensions, 'code': code}), encoding='utf8', timeout=300, check=True,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            result['browser'] = json.loads((api.root / 'browser.json').read_text(encoding='utf8'))
            events = api.call('GET', f'/conversations/{cid}')['events']
            users = [event['payload'] for event in events if event['type'] == 'message' and event['payload']['role'] == 'user']
            assert len(users) == 2 and not users[1].get('images')
            image, = users[0]['images']
            assert image['name'] == 'equipment.png' and image['mime'] == 'image/png' and image['size'] == len(content)
            assert 'data' not in image
            raw = api.prefix + '/raw?' + urlencode({'path': image['path']})
            assert api.call('GET', raw, raw=True) == content
            outsider = api.call('POST', '/auth/register', {'email': 'f67-outsider@example.com',
                'password': 'test-other-account-123', 'name': 'Outsider'})
            headers = {'Authorization': 'Bearer ' + outsider['token']}
            api.call('GET', raw, expect=404, extra_headers=headers)
            api.call('GET', f'/conversations/{cid}', expect=404, extra_headers=headers)
            result.update(image_sha256=hashlib.sha256(content).hexdigest(), original_bytes_retained=True, cross_account_status=404)
        result['api_exit'] = wait_for_api_exit(api.root, api.port)
        trace = api.root / 'upstream.jsonl'
        rows = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
        actual = [row for row in rows if row['kind'] == 'http' and row['path'].endswith('/chat/completions')]
        assert len(actual) == 2 and all(row['status'] == 200 for row in actual), actual
        digests = [row['sha256'] for row in rows if row['kind'] == 'request_image_digests']
        assert digests == [[result['image_sha256']], [result['image_sha256']]], digests
        result['passed'] = True
    finally:
        trace = api.root / 'upstream.jsonl'
        result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        evidence = json.dumps(result, ensure_ascii=False, indent=2)
        assert config['key'] not in evidence
        (api.root / 'image-result.json').write_text(evidence, encoding='utf8')


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        args = parser.parse_args()
        run(args, asyncio.run(source(args.source_profile_id)))
