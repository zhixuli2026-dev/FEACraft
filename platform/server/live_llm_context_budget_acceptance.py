"""Check bounded model input, exact provider usage, and long history replay with DeepSeek."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from time import perf_counter
from urllib.parse import parse_qs, urlsplit

import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from testsupport import IsolatedServer


CASE = 'Long conversation model window'
COUNT = 6000
INPUT_LIMIT = 12000


def browser(data):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1520, 'height': 950})
        context.add_init_script(f"if (location.origin === {json.dumps(data['base'])}) localStorage.setItem('vf_token', {json.dumps(data['token'])});")
        page = context.new_page()
        posts = []
        page.on('request', lambda req: posts.append(req.method) if req.method == 'POST' and
                re.search(r'/conversations/\d+/messages$', req.url) else None)
        page.goto(data['base'], wait_until='domcontentloaded')
        page.locator('[data-case-name=' + json.dumps(CASE) + ']').click(timeout=30000)
        chooser = page.get_by_test_id('agent-conversation')
        if not chooser.is_visible():
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
        chooser.select_option(str(data['cid']))
        nav = page.get_by_test_id('chat-user-nav').locator('option')
        expect(page.get_by_test_id('chat-markdown').get_by_text(data['marker'], exact=True)).to_be_visible()
        baseline = nav.count()
        assert 1 < baseline <= 281
        pages = []
        for step in range(2):
            before = perf_counter()
            with page.expect_response(lambda res: urlsplit(res.url).path == f'/api/v1/conversations/{data["cid"]}'
                                      and 'offset=' in res.url) as next_page:
                page.get_by_test_id('chat-load-older').click()
            reply = next_page.value
            assert reply.ok
            query = parse_qs(urlsplit(reply.url).query)
            expect(nav).to_have_count(baseline+(step+1)*280)
            pages.append({'offset': int(query['offset'][0]), 'snapshot': int(query['snapshot_id'][0]),
                          'milliseconds_to_render': round((perf_counter()-before)*1000)})
        assert [p['offset'] for p in pages] == [280, 560] and len({p['snapshot'] for p in pages}) == 1
        assert not posts
        root = Path(data['root'])
        page.screenshot(path=root / 'history-replay.png')
        (root / 'browser.json').write_text(json.dumps({'passed':True,'edge':edge.version,'first_visible_options':baseline,
            'pages':pages,'inference_requests_from_replay':len(posts)}, indent=2), encoding='utf8')
        edge.close()


def verify(args, config):
    api = IsolatedServer('live-f17-model-context', app='context_fixture:app')
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    fixture = f'import os\nos.environ["VIBEFLOW_AGENT_CONTEXT_INPUT_TOKEN_CAP"]={str(INPUT_LIMIT)!r}\nlive_request_limit=1\n' + FIXTURE
    fixture += '''
from cryptography.fernet import Fernet
from app.services.process_identity_native import observe
vault=Fernet(Fernet.generate_key())
llm._vault=lambda:vault
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())),encoding='utf8')
forward=httpx.AsyncClient.send
async def observe_request(self,request,*args,**kwargs):
 if request.url.host=='api.deepseek.com' and request.url.path.endswith('/chat/completions'):
  body=json.loads(request.content)
  users=[m['content'] for m in body['messages'] if m['role']=='user']
  receipt({'kind':'selection','user_messages':len(users),'json_bytes':len(request.content),
           'latest_present':'CURRENT_WINDOW_' in str(users[-1]),
           'oldest_present':any('HISTORY_WINDOW_00001' in str(m) for m in users),
           'excluded_media_present':any('EXCLUDED_MISSING_ATTACHMENT' in str(m) for m in users),
           'reserved_output':body.get('max_tokens')})
 return await forward(self,request,*args,**kwargs)
httpx.AsyncClient.send=observe_request
'''
    fixture += f'\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n'
    (api.root / 'context_fixture.py').write_text(fixture, encoding='utf8')
    report = {'passed':False,'model':config['model'],'mock_llm_calls':0}
    print('LIVE_F17_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case=api.create_case(CASE)
            profile=api.call('POST','/provider-profiles',{'name':'Authorized DeepSeek context','provider':'deepseek',
                'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})['id']
            old = []
            for counter in range(1,COUNT+1):
                payload={'role':'user','content':f'HISTORY_WINDOW_{counter:05} archived pressure log. ' + 'pressure gradient inlet ' * 19}
                if counter == COUNT - 40:
                    payload['content'] += 'EXCLUDED_MISSING_ATTACHMENT'
                    payload['images']=[{'mime':'image/png','name':'old.png','path':'.vibeflow/attachments/unavailable/old.png','size':256}]
                old.append(json.dumps({'type':'message','payload':payload}))
            auth={'Authorization':'Bearer '+api.token}
            with httpx.Client(base_url=api.base+'/api/v1', headers=auth, trust_env=False, timeout=110) as client:
                imported=client.post('/conversations:import',params={'case_id':case['id'],'title':'Bounded archive'},
                                     content='\n'.join(old).encode('utf8'))
                imported.raise_for_status()
                cid=imported.json()['id']
                api.call('PATCH',f'/conversations/{cid}/settings',{'model':config['model'],'provider_profile_id':profile,
                    'permission_mode':'ask'})
                marker='CURRENT_WINDOW_'+secrets.token_hex(4)
                def send(text):
                    response=client.post(f'/conversations/{cid}/messages',json={'content':text,'provider_profile_id':profile})
                    response.raise_for_status()
                    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
                frames=send('Reply with exactly '+marker+' and no other text.')
                answer=''.join(e['text'] for e in frames if e['type']=='text.delta').strip()
                assert answer==marker and any(e['type']=='run.completed' for e in frames),answer
                selected=next(e for e in frames if e['type']=='context.window')
                assert selected['excluded_turns']>900 and 1 < selected['retained_turns'] < 30
                assert selected['input_limit']==INPUT_LIMIT and selected['input_estimate']<=INPUT_LIMIT
                session=api.call('GET',f'/conversations/{cid}')
                latest=session['latest_model_usage']
                assert latest['status']=='reported' and latest['input_selection']['excluded_turns']==selected['excluded_turns']
                assert latest['counts']['input_tokens'] < selected['input_estimate']
                assert len(session['events'])==280 and session['event_window']['total']>COUNT
                export=client.get(f'/conversations/{cid}/export');export.raise_for_status()
                assert 'HISTORY_WINDOW_00001' in export.text and marker in export.text
                report.update(actual_input_tokens=latest['counts']['input_tokens'], context_selection=selected,
                              archive_events_before_turn=COUNT, total_events=session['event_window']['total'])

                temp=api.root/'edge-tmp';temp.mkdir()
                subprocess.run([str(args.browser_python),'-B','-X','utf8',__file__,'--browser'],cwd=api.root,
                    timeout=130,encoding='utf8',check=True,env=dict(os.environ,TEMP=str(temp),TMP=str(temp)),
                    input=json.dumps({'root':str(api.root),'base':api.base,'token':api.token,'cid':cid,'marker':marker}))
                report['browser']=json.loads((api.root/'browser.json').read_text(encoding='utf8'))
                limit=send('current oversized input: '+'Z'*14000)
                assert any(e.get('code')=='context_too_large' and e.get('status')==413 for e in limit)
                assert not any(e['type']=='request.usage' for e in limit)
                report['oversize_rejected_without_provider']=True
        report['api_exit']=wait_for_api_exit(api.root,api.port)
        trace=api.root/'upstream.jsonl'
        rows=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
        selected=[r for r in rows if r['kind']=='selection']
        assert len(selected)==1 and selected[0]['latest_present'] and not selected[0]['oldest_present']
        assert not selected[0]['excluded_media_present'] and selected[0]['reserved_output']==384000
        assert sum(r['kind']=='http' and r['status']==200 for r in rows)==1
        usage=next(r['usage'] for r in rows if r['kind']=='stream_identity')
        assert usage['prompt_tokens']==report['actual_input_tokens']
        report['passed']=True
    finally:
        trace=api.root/'upstream.jsonl'
        report['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        text=json.dumps(report,ensure_ascii=False,indent=2)
        assert config['key'] not in text
        (api.root/'context-result.json').write_text(text,encoding='utf8')


if __name__=='__main__':
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id',type=int,required=True)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        args=parser.parse_args()
        verify(args,asyncio.run(source(args.source_profile_id)))
