"""Verify Ask sanitization and execution policy against the official DeepSeek APIs."""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import httpx

from app.services.sanitizer import ProtocolSanitizer, sanitize_text
from live_llm_acceptance import FIXTURE as LIVE, source
from testsupport import IsolatedServer


FILE = 'Ref/ask-sanitizer-proof.txt'
SAMPLE = 'SAMPLE_START<｜DSML｜tool_calls><｜DSML｜invoke name="example">BLOCKED_TEXT_33</｜DSML｜invoke></｜DSML｜tool_calls>SAMPLE_END'
OBSERVER = '''
from app.services.sanitizer import sanitize_text
from app.services.tools import tools_for_openai, tools_for_anthropic

observed_send = httpx.AsyncClient.send
observed_lines = httpx.Response.aiter_lines

def stage():
    return (root/'stage').read_text(encoding='utf-8')

async def inspect_send(self, request, *args, **kwargs):
    if request.url.host != 'api.deepseek.com':
        return await observed_send(self, request, *args, **kwargs)
    label = stage()
    body = json.loads(request.content)
    is_messages = request.url.path.endswith('/messages')
    previous = [message.get('content') for message in body['messages'] if message['role']=='assistant']
    previous_text = []
    for content in previous:
        if isinstance(content,str):
            previous_text.append(content)
        elif isinstance(content,list):
            previous_text.extend(block['text'] for block in content if block.get('type')=='text')
    captured = None
    if label=='replay':
        raw=json.loads((root/'actual-provider-literal.json').read_text(encoding='utf-8'))
        captured=sanitize_text(raw['content'])
    receipt({'kind':'ask_request','stage':label,'tools_originally_offered':len(body.get('tools',[])),
             'assistant_history_clean':all(sanitize_text(text)==text for text in previous_text),
             'assistant_history_seen':bool(previous_text),
             'assistant_history_matches_cleaned_capture':captured in previous_text})
    if label=='forbidden':
        # Ask the real service for a structured tool call after observing the application's actual request.
        name='propose_file_update'
        if is_messages:
            body['tools']=[tool for tool in tools_for_anthropic() if tool['name']==name]
            body['tool_choice']={'type':'tool','name':name}
        else:
            body['tools']=[tool for tool in tools_for_openai() if tool['function']['name']==name]
            body['tool_choice']={'type':'function','function':{'name':name}}
        assert len(body['tools'])==1
        body['thinking']={'type':'disabled'}
        body.pop('reasoning_effort',None)
        headers=dict(request.headers)
        headers.pop('content-length',None)
        request=httpx.Request(request.method,request.url,json=body,headers=headers,extensions=request.extensions)
    return await observed_send(self, request, *args, **kwargs)

async def inspect_lines(self, *args, **kwargs):
    if self.request.url.host!='api.deepseek.com':
        async for line in observed_lines(self, *args, **kwargs):
            yield line
        return
    label=stage()
    chunks, reasoning, tools=[], [], set()
    try:
        async for line in observed_lines(self, *args, **kwargs):
            if line.startswith('data:') and line[5:].strip() not in ('','[DONE]'):
                event=json.loads(line[5:])
                for choice in event.get('choices',[]):
                    delta=choice.get('delta') or {}
                    if delta.get('content'): chunks.append(delta['content'])
                    if delta.get('reasoning_content'): reasoning.append(delta['reasoning_content'])
                    for tool in delta.get('tool_calls') or []:
                        if name:=(tool.get('function') or {}).get('name'): tools.add(name)
                if event.get('type')=='content_block_delta':
                    delta=event['delta']
                    if delta.get('text'): chunks.append(delta['text'])
                    if delta.get('thinking'): reasoning.append(delta['thinking'])
                if event.get('type')=='content_block_start' and event.get('content_block',{}).get('type')=='tool_use':
                    tools.add(event['content_block']['name'])
            yield line
    finally:
        body=''.join(chunks)
        thought=''.join(reasoning)
        receipt({'kind':'raw_observation','stage':label,'text_parts':len(chunks),'reasoning_parts':len(reasoning),
                 'structured_tools':sorted(tools),'literal_hidden_in_text':'BLOCKED_TEXT_33' in body,
                 'literal_hidden_in_reasoning':'BLOCKED_TEXT_33' in thought})
        if label=='literal':
            (root/'actual-provider-literal.json').write_text(json.dumps({'content':body,'reasoning':thought},ensure_ascii=False),encoding='utf-8')

httpx.AsyncClient.send = inspect_send
httpx.Response.aiter_lines = inspect_lines
'''


def verify_protocol_text():
    cases = [
        ('A<tool_call>discard</tool_call>B<update_plan />C', 'ABC'),
        ('A<|dsml|>unfinished', 'A'),
        ('A<|dsml|>ignore<|dsml_end|>B', 'AB'),
        ('<note>ok</note><tool_callish>keep</tool_callish>', '<note>ok</note><tool_callish>keep</tool_callish>'),
        ('ordinary <｜DSML｜...> placeholder', 'ordinary <｜DSML｜...> placeholder'),
        (SAMPLE, 'SAMPLE_STARTSAMPLE_END'),
        ('A<｜DSML｜function_calls>ignore</｜DSML｜function_calls>B', 'AB'),
        ('A<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>ignore<｜tool▁call▁end｜><｜tool▁calls▁end｜>B', 'AB'),
        ('A<｜tool▁call▁begin｜>ignore<｜tool▁call▁end｜>B', 'AB'),
        ('A</｜DSML｜invoke>B</tool_call>C', 'ABC'),
        ('A<tool_call name="' + 'x'*400 + '">discard</tool_call>B', 'AB'),
        ('A<tool_call name="a>b"/>B<function_call>ignore</function_call>C', 'ABC'),
        ('A<tool_call name="' + 'x'*400, 'A'),
    ]
    count = 0
    for sample, expected in cases:
        assert sanitize_text(sample) == expected
        for split in range(len(sample)+1):
            sanitizer = ProtocolSanitizer()
            assert sanitizer.feed(sample[:split]) + sanitizer.feed(sample[split:]) + sanitizer.finish() == expected, split
            count += 1
        sanitizer = ProtocolSanitizer()
        assert ''.join(sanitizer.feed(letter) for letter in sample) + sanitizer.finish() == expected
    bounded = ProtocolSanitizer()
    assert bounded.feed('before<tool_call>') == 'before'
    for _ in range(32):
        assert bounded.feed('x'*4096) == '' and len(bounded._buffer) <= 256
    assert bounded.feed('</tool_call>after') + bounded.finish() == 'after'
    return {'protocol_cases':len(cases),'two_chunk_splits':count,'one_character_streams':len(cases),
            'excluded_body_buffer_bounded':True}


def verify(configuration, wire):
    api = IsolatedServer('live-f33-ask-sanitizer', app='ask_sanitizer_fixture:app')
    (api.root/'ask_sanitizer_fixture.py').write_text(LIVE+OBSERVER,encoding='utf-8')
    result = {'passed':False,'wire':wire,'model':configuration['model'],'mock_llm_calls':0, **verify_protocol_text()}
    print('LIVE_F33_SANITIZER',api.root,flush=True)

    def turn(cid, profile, label, content, effort='off'):
        (api.root/'stage').write_text(label,encoding='utf-8')
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=90,trust_env=False) as client:
            response = client.post(f'/conversations/{cid}/messages',json={'provider_profile_id':profile,
                'model':configuration['model'],'content':content,'reasoning_effort':effort})
            response.raise_for_status()
        assert configuration['key'] not in response.text
        (api.root/(label+'-public.sse')).write_text(response.text,encoding='utf-8')
        return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]

    def assistant(cid):
        history=api.call('GET',f'/conversations/{cid}')
        messages=[event for event in history['events'] if event['type']=='message' and event['payload'].get('role')=='assistant']
        return messages[-1]

    try:
        with api:
            case=api.create_case('FF-F33 actual Ask sanitation')
            api.put(FILE,'ORIGINAL_FILE')
            profile=api.call('POST','/provider-profiles',{'provider':'anthropic' if wire=='messages' else 'deepseek',
                'name':'Authorized DeepSeek sanitizer testing','default_model':configuration['model'],'api_key':configuration['key'],
                'base_url':'https://api.deepseek.com/anthropic' if wire=='messages' else configuration['base_url']})['id']
            conv=api.call('POST','/conversations',{'case_id':case['id'],'title':'Actual Ask sanitation',
                'permission_mode':'ask','provider_profile_id':profile,'model':configuration['model']})['id']
            literal=turn(conv,profile,'literal','请说明这段文字的标签结构和正文边界。不需要调用工具。\n'+SAMPLE,'medium')
            streamed=''.join(frame['text'] for frame in literal if frame['type']=='text.delta')
            streamed_reasoning=''.join(frame['text'] for frame in literal if frame['type']=='reasoning.delta')
            raw=json.loads((api.root/'actual-provider-literal.json').read_text(encoding='utf-8'))
            assert any(sanitize_text(value)!=value for value in raw.values()), 'Provider did not include recognized literal DSML'
            assert configuration['key'] not in json.dumps(raw)
            assert streamed and streamed==sanitize_text(raw['content'])
            assert streamed_reasoning==sanitize_text(raw['reasoning'])
            message=assistant(conv)
            assert message['payload']['content']==streamed
            assert message['payload'].get('reasoning','')==streamed_reasoning
            result['actual_literal_stream_and_persistence_clean']=True

            # Replay a legacy persisted row using raw text from this actual provider response.
            original=message['payload']
            original.update(content=raw['content'],reasoning=raw['reasoning'])
            uri=(api.root/'test.sqlite').resolve().as_uri()+'?mode=rw'
            with closing(sqlite3.connect(uri,uri=True,timeout=3)) as db:
                changed=db.execute('UPDATE agent_events SET payload=? WHERE id=?',(json.dumps(original,ensure_ascii=False),message['id']))
                assert changed.rowcount==1
                db.commit()
            replayed=assistant(conv)['payload']
            assert replayed['content']==streamed and replayed.get('reasoning','')==streamed_reasoning
            follow=turn(conv,profile,'replay','Return only FOLLOW_UP_33 as plain text without invoking tools.')
            assert 'FOLLOW_UP_33' in ''.join(frame['text'] for frame in follow if frame['type']=='text.delta')
            result['legacy_read_and_real_resend_clean']=True

            # A separate Ask conversation must refuse a genuine structured response without materializing a tool call.
            forbidden=api.call('POST','/conversations',{'case_id':case['id'],'title':'Actual forbidden tool proof',
                'permission_mode':'ask','provider_profile_id':profile,'model':configuration['model']})['id']
            frames=turn(forbidden,profile,'forbidden',f'请只调用 propose_file_update，path 为 {FILE}，content 为 SHOULD_NOT_WRITE。')
            assert any(event.get('code')=='tool_permission_denied' for event in frames), frames
            assert not any(event['type'] in ('tool.result','tool.state','approval.requested') for event in frames)
            history=api.call('GET',f'/conversations/{forbidden}')
            assert not history['tool_calls'] and not history['pending_tools']
            assert api.call('GET',api.prefix+'/file?path='+FILE)['content']=='ORIGINAL_FILE'
            rows=[json.loads(line) for line in (api.root/'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
            requests={row['stage']:row for row in rows if row['kind']=='ask_request'}
            assert set(requests)=={'literal','replay','forbidden'} and all(row['tools_originally_offered']==0 for row in requests.values())
            assert all(requests['replay'][field] for field in ('assistant_history_seen','assistant_history_clean',
                                                               'assistant_history_matches_cleaned_capture'))
            wire_events={row['stage']:row for row in rows if row['kind']=='raw_observation'}
            assert wire_events['forbidden']['structured_tools']==['propose_file_update']
            http=[row for row in rows if row['kind']=='http']
            assert len(http)==3 and all(row['status']==200 for row in http)
            result.update(real_http_200=len(http), forbidden_real_tool_no_effect=True,
                provider_reasoning_contained_test_marker=wire_events['literal']['literal_hidden_in_reasoning'])
        assert api.proc.poll() is not None
        result.update(passed=True,isolated_api_exited=True)
    finally:
        result_path=api.root/'live-result.json'
        result_path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print('LIVE_F33_SANITIZER_RESULT',result_path,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol-only',action='store_true')
    parser.add_argument('--source-profile-id',type=int)
    parser.add_argument('--wire',choices=('completions','messages'),default='completions')
    args=parser.parse_args()
    if args.protocol_only:
        print(json.dumps(verify_protocol_text()))
    else:
        if args.source_profile_id is None:
            parser.error('--source-profile-id is required for live inference')
        verify(asyncio.run(source(args.source_profile_id)),args.wire)
