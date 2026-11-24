"""Submit a scoped case-file attachment from Edge to the authorized DeepSeek profile."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import uuid


CASE = 'A26 scoped case attachment'
FILE = 'Ref/path-range-proof.txt'
WATCH = r'''
import hashlib
import os
from fastapi import Depends
from fastapi.staticfiles import StaticFiles
from app.deps import get_current_user
from app.services.process_identity_native import observe

proof = json.loads((root/'scope-proof.json').read_text(encoding='utf-8'))
forward_live = httpx.AsyncClient.send

async def observe_attachment(self, request, *args, **kwargs):
    if request.url.host == 'api.deepseek.com' and request.url.path.endswith('/chat/completions'):
        data = json.loads(request.content)
        messages = data['messages']
        sent = [item['content'] for item in messages if item['role']=='user']
        encoded = json.dumps(messages, ensure_ascii=False)
        receipt({'kind':'request_scope','scheme':request.url.scheme,'host':request.url.host,
                 'path':request.url.path,'model':data['model'],
                 'user_content_sha256':[hashlib.sha256(item.encode()).hexdigest() for item in sent],
                 'attachment_exact':sent==[proof['expected_message']],
                 'selected_markers':[marker in encoded for marker in proof['selected']],
                 'excluded_present':[marker in encoded for marker in proof['excluded']]})
    return await forward_live(self, request, *args, **kwargs)

httpx.AsyncClient.send = observe_attachment

@app.get('/fixture/a26/process')
def process(user=Depends(get_current_user)):
    return observe(os.getpid())

@app.post('/fixture/a26:stop')
def stop(data: dict, user=Depends(get_current_user)):
    assert data['pid'] == os.getpid()
    os._exit(79)
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def scope_proof():
    nonce = uuid.uuid4().hex[:10]
    selected = [f'A26-{nonce}-one', f'A26-{nonce}-two']
    excluded = [f'outside-{index}-{nonce}' for index in range(6)]
    lines = [excluded[0], f'{excluded[1]} {selected[0]} {excluded[2]}',
             f'{excluded[3]} {selected[1]} {excluded[4]}', excluded[5]]
    start = len(excluded[1])+2
    end = start+len(selected[0])-1
    scope = f'{FILE}:2-3:{start}-{end}'
    prompt = ('Read the two identifiers in the attachment. Reply with exactly those two identifiers '
              'in the same order on separate lines. Copy each character. No tools.\n@'+scope)
    expected = prompt+'\n\n[附件 '+scope+']\n'+chr(96)*3+'\n'+'\n'.join(selected)+'\n'+chr(96)*3
    return {'scope':scope, 'fixture_text':'\n'.join(lines), 'selected':selected,
            'excluded':excluded, 'prompt':prompt, 'expected_message':expected}


async def browser(data):
    from playwright.async_api import async_playwright, expect

    root, base = Path(data['root']), data['base']
    route = f'/api/v1/conversations/{data["conversation_id"]}/messages'
    report = {'rejected':[], 'message_posts':0}
    submitted = []

    async with async_playwright() as play:
        edge = await play.chromium.launch(channel='msedge')
        context = await edge.new_context(locale='zh-CN', viewport={'width':1700,'height':1000},
            storage_state={'cookies':[], 'origins':[{'origin':base,
                'localStorage':[{'name':'vf_token','value':data['token']}]}]})
        page = await context.new_page()

        def is_message(request):
            return request.method=='POST' and urlsplit(request.url).path==route

        def observe(request):
            if is_message(request):
                submitted.append(request.post_data_json)

        page.on('request', observe)
        try:
            await page.goto(base,wait_until='domcontentloaded')
            await page.locator('[data-case-name='+json.dumps(CASE)+']').click(timeout=25000)
            await page.locator('.dv-tab').filter(has_text=re.compile(r'^Agent$')).click()
            await expect(page.get_by_test_id('agent-conversation')).to_have_value(str(data['conversation_id']))
            await expect(page.get_by_test_id('agent-profile')).to_have_value(str(data['profile_id']))
            entry, send = page.get_by_test_id('agent-input'),page.get_by_test_id('agent-send')
            for suffix, detail in (
                (':abc','行列范围格式无效'),
                (':3-2','行范围无效'),
                (':8','引用行号越界'),
                (':2-3:2-512','引用列号越界'),
            ):
                draft = 'Reject this attachment: @'+FILE+suffix
                await entry.fill(draft)
                await send.click()
                await expect(page.get_by_text(f'[附件错误] {FILE}: {detail}',exact=False)).to_be_visible()
                await expect(entry).to_have_value(draft)
                assert not submitted
                report['rejected'].append({'reference':FILE+suffix,'reason':detail,'message_posts':len(submitted)})
            report['pre_inference_rejections'] = len(report['rejected'])
            await entry.fill(data['prompt'])
            async with page.expect_response(lambda response: is_message(response.request),timeout=90000) as pending:
                await send.click()
            response = await pending.value
            assert response.status == 200
            events = [json.loads(line[5:]) for line in (await response.text()).splitlines() if line.startswith('data:')]
            assert not any(item['type'] in {'request.failed','request.timed_out','stream.error'} for item in events), events
            assert any(item['type']=='run.completed' for item in events), events
            assert len(submitted)==1 and submitted[0]['content']==data['expected_message']
            reply = ''.join(item['text'] for item in events if item['type']=='text.delta')
            assert all(marker in reply for marker in data['selected'])
            assert all(marker not in reply for marker in data['excluded'])
            await expect(entry).to_have_value('')
            report.update({'answered':True,'answer':reply,'browser_version':edge.version,
                           'sent_sha256':digest(submitted[0]['content'].encode())})
        finally:
            report['message_posts']=len(submitted)
            (root/'browser-path-range.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
            await context.close()
            await edge.close()


def verify(args):
    import httpx
    from app.services.process_identity_native import probe
    from live_llm_acceptance import FIXTURE, source
    from testsupport import IsolatedServer, SERVER

    config = asyncio.run(source(args.source_profile_id))
    assert urlsplit(config['base_url']).scheme=='https'
    api = IsolatedServer('live-path-range',app='live_fixture:app')
    bundle = args.dist.resolve(strict=True)
    index = bundle/'index.html'
    main = bundle/re.search(r'\bsrc="/([^"]+\.js)"',index.read_text(encoding='utf-8')).group(1)
    temporary = api.root/'temporary'
    temporary.mkdir()
    os.environ.update(TEMP=str(temporary), TMP=str(temporary))
    old_limit = "if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')"
    assert FIXTURE.count(old_limit)==1
    fixture = FIXTURE.replace(old_limit,"if calls>1:raise RuntimeError('A26 permits one official DeepSeek request')")
    fixture += WATCH+"\napp.mount('/', StaticFiles(directory="+repr(str(bundle))+", html=True))\n"
    (api.root/'live_fixture.py').write_text(fixture,encoding='utf-8')
    proof = scope_proof()
    (api.root/'scope-proof.json').write_text(json.dumps(proof,ensure_ascii=False),encoding='utf-8')
    trace = api.root/'upstream.jsonl'
    report = {'passed':False,'provider':config['provider'],'model':config['model'],'source_profile_id':26,
              'mock_llm_calls':0,'index_sha256':digest(index.read_bytes()),'main_sha256':digest(main.read_bytes())}

    def upstream():
        return [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []

    print('A26_REAL_PATH_RANGE '+str(api.root),flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            process = api.call('GET','/fixture/a26/process',api_prefix=False)
            try:
                api.put(FILE,proof['fixture_text'])
                profile = api.call('POST','/provider-profiles',{'provider':config['provider'],'name':'A26 authorized profile',
                    'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
                assert config['key'] not in json.dumps(profile)
                conversation = api.call('POST','/conversations',{'case_id':case['id'],'title':'A26 case-file scope',
                    'permission_mode':'ask','provider_profile_id':profile['id'],'model':config['model']})
                browser_args = {**proof,'root':str(api.root),'base':api.base,'token':api.token,
                    'profile_id':profile['id'],'conversation_id':conversation['id']}
                subprocess.run([str(args.browser_python.resolve(strict=True)),'-B','-X','utf8',str(Path(__file__).resolve()),
                    '--browser'],input=json.dumps(browser_args),text=True,timeout=150,check=True)
                observed = json.loads((api.root/'browser-path-range.json').read_text(encoding='utf-8'))
                attempts = [event for event in upstream() if event['kind']=='request_scope']
                assert len(attempts)==1 and attempts[0]['attachment_exact']
                assert attempts[0]['scheme']=='https' and attempts[0]['host']=='api.deepseek.com'
                assert all(attempts[0]['selected_markers']) and not any(attempts[0]['excluded_present'])
                assert attempts[0]['user_content_sha256']==[observed['sent_sha256']]
                calls = [event for event in upstream() if event['kind']=='http']
                assert len(calls)==1 and calls[0]['status']==200 and calls[0]['path'].endswith('/chat/completions')
                identity = [event for event in upstream() if event['kind']=='stream_identity']
                assert len(identity)==1 and identity[0]['id'] and identity[0]['usage']['total_tokens']>0
                history = api.call('GET',f'/conversations/{conversation["id"]}')
                rows = [event['payload'] for event in history['events'] if event['type']=='message']
                assert [(row['role'],row['content']) for row in rows]==[
                    ('user',proof['expected_message']),('assistant',observed['answer'])]
                report.update({'browser':observed,'persisted_messages':len(rows),'scope':proof['scope']})
            finally:
                with httpx.Client(trust_env=False,timeout=10) as client:
                    try:
                        client.post(api.base+'/fixture/a26:stop',json={'pid':process['pid']},
                                    headers={'Authorization':'Bearer '+api.token})
                    except httpx.TransportError:
                        pass
                api.proc.wait(timeout=12)
        report['api_exit'] = probe(process)
        assert report['api_exit']['state']=='exited'
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,
                           'message':str(error).replace(config['key'],'[REDACTED]')[:1200]}
        raise RuntimeError('A26 real browser verification failed. See '+str(api.root)) from None
    finally:
        report['upstream']=upstream()
        report['real_model_requests']=sum(item['kind']=='http' and item['path'].endswith('/chat/completions')
                                          for item in report['upstream'])
        report['real_model_attempts']=sum(item['kind']=='request_scope' for item in report['upstream'])
        paths=(Path(__file__).resolve(),SERVER/'live_llm_acceptance.py',SERVER.parent/'web/src/utils/pathRefs.ts',
               SERVER.parent/'web/src/panels/ChatPanel.tsx')
        report['source_sha256']={path.relative_to(SERVER.parent).as_posix():digest(path.read_bytes()) for path in paths}
        output=json.dumps(report,indent=2,ensure_ascii=False)
        assert config['key'] not in output
        (api.root/'live-result.json').write_text(output,encoding='utf-8')
        if api.proc is not None and api.proc.poll() is not None:
            (api.root/'.vault_key').unlink(missing_ok=True)
    print('A26_REAL_PATH_RANGE_OK '+str(api.root),flush=True)


if __name__=='__main__':
    if '--browser' in sys.argv:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id',type=int,choices=[26],required=True)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        verify(parser.parse_args())
