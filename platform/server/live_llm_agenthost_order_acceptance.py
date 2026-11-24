"""FF-F52: actual DeepSeek lanes, sparse flush, approval order and HTTP disconnect."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time
from urllib.parse import urlencode
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


FILE = 'Ref/agenthost-stream.txt'
OLD = 'F52_APPROVAL_BEFORE'
NEW = 'F52_APPROVAL_AFTER'
LANES = ('reasoning.delta', 'text.delta', 'tool.delta')

OBSERVE = '''
import asyncio
import hashlib
import os
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Conversation, ConversationExecution
from app.routers import agent
from app.routers.workspaces import get_owned_case
from app.services.process_identity_native import observe
isolated_vault = Fernet(Fernet.generate_key())
llm._vault = lambda: isolated_vault
(root / 'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')

@app.get('/api/v1/fixture/stream-owner/{cid}/{run_id}')
async def owner_state(cid: int, run_id: str, user=Depends(get_current_user)):
    async with SessionLocal() as db:
        conversation = await db.get(Conversation, cid)
        if conversation is None:
            raise HTTPException(404)
        await get_owned_case(conversation.case_id, user, db, write=False)
        row = await db.get(ConversationExecution, cid)
        return {'phase': row.phase if row else None, 'registered': cid in agent._RUN_CANCELLATIONS,
            'sequence_tracked': run_id in agent._SSE_SEQ}

previous_lines = httpx.Response.aiter_lines
stream_count = 0

async def observe_lanes(self, *args, **kwargs):
    global stream_count
    if self.request.url.host != 'api.deepseek.com':
        async for line in previous_lines(self, *args, **kwargs):
            yield line
        return
    stream_count += 1
    stream_index = stream_count
    record = {'index': stream_index, 'provider_completion_id': None, 'done': False, 'groups': []}
    groups = []
    pause_after_this_line = False
    credential = self.request.headers.get('Authorization', '').removeprefix('Bearer ')

    def fragment(kind, text):
        if not groups or groups[-1]['type'] != kind:
            groups.append({'type': kind, 'fragments': 0, 'characters': 0, 'digest': hashlib.sha256()})
        row = groups[-1]
        row['digest'].update(text.encode('utf8'))
        row['characters'] += len(text)
        row['fragments'] += 1

    try:
        async for line in previous_lines(self, *args, **kwargs):
            if line.startswith('data:'):
                value = line[5:].strip()
                if value == '[DONE]':
                    record['done'] = True
                else:
                    try:
                        data = json.loads(value)
                    except ValueError:
                        data = {}
                    record['provider_completion_id'] = data.get('id') or record['provider_completion_id']
                    choice = (data.get('choices') or [{}])[0]
                    delta = choice.get('delta') or {}
                    reasoning = delta.get('reasoning_content') or ''
                    if reasoning:
                        fragment('reasoning.delta', reasoning)
                        pause_after_this_line = (probe_sparse and stream_index == 1 and 'pause' not in record and '<' not in reasoning
                            and not (delta.get('content') or delta.get('tool_calls') or choice.get('finish_reason')))
                    if delta.get('content'):
                        fragment('text.delta', delta['content'])
                    for tool in delta.get('tool_calls') or []:
                        fragment('tool.delta', '')
            if pause_after_this_line:
                record['pause'] = {'forwarded_at_ns': time.monotonic_ns()}
            if credential and credential in line:
                raise RuntimeError('Credential appeared in an actual provider response')
            yield line
            if pause_after_this_line:
                # Release the current real fragment, then defer reading the next
                # actual fragment. No model content or ordering is substituted.
                await asyncio.sleep(.22)
                record['pause']['next_allowed_at_ns'] = time.monotonic_ns()
                pause_after_this_line = False
    finally:
        record['groups'] = [{'type': row['type'], 'fragments': row['fragments'], 'characters': row['characters'],
            'sha256': row['digest'].hexdigest()} for row in groups]
        with (root / 'upstream-lanes.jsonl').open('a', encoding='utf8') as log:
            log.write(json.dumps(record) + '\\n')

httpx.Response.aiter_lines = observe_lanes
'''


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []


def digest(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def lane_groups(frames):
    groups = []
    for frame in frames:
        lane = frame['type']
        if lane not in LANES:
            continue
        if not groups or groups[-1]['type'] != lane:
            groups.append({'type': lane, 'parts': []})
        groups[-1]['parts'].append(frame.get('text', ''))
    return [{'type': row['type'], 'fragments': len(row['parts']), 'characters': len(''.join(row['parts'])),
             'sha256': digest(''.join(row['parts']))} for row in groups]


def compare(upstream, downstream):
    assert upstream['done'] and upstream['provider_completion_id']
    received = lane_groups(downstream)
    assert [row['type'] for row in received] == [row['type'] for row in upstream['groups']]
    combined_frames = 0
    for old, new in zip(upstream['groups'], received):
        assert (old['characters'], old['sha256']) == (new['characters'], new['sha256']), (old, new)
        if old['type'] == 'tool.delta':
            assert old['fragments'] == new['fragments'], (old, new)
        else:
            combined_frames += old['fragments'] - new['fragments']
    return {'upstream_id': upstream['provider_completion_id'], 'lanes': received, 'combined_frames': combined_frames}


def ordered(frames):
    assert frames
    run_id = frames[0].get('run_id')
    assert isinstance(run_id, str) and re.fullmatch('[0-9a-f]{32}', run_id), (frames[0]['type'], run_id)
    assert all(frame.get('run_id') == run_id for frame in frames)
    assert [frame.get('seq') for frame in frames] == list(range(1, len(frames) + 1))
    return run_id


def verify(configuration, phase):
    from app.services.process_identity_native import probe

    api = IsolatedServer('live-f52-agenthost-order', app='agenthost_order_fixture:app', access_log=True)
    budget = 5 if phase == 'all' else 2
    setup = FIXTURE.replace('if calls>8:', f'if calls>{budget}:') + f'\nprobe_sparse = {phase == "all"}\n'
    (api.root / 'agenthost_order_fixture.py').write_text(setup + OBSERVE, encoding='utf8')
    report = {'passed': False, 'provider': 'deepseek', 'host': 'api.deepseek.com',
              'model': configuration['model'], 'mock_llm_calls': 0, 'phase': phase}
    print('LIVE_F52_EVIDENCE', api.root, flush=True)

    def save():
        upstream = jsonl(api.root / 'upstream.jsonl')
        report['upstream'] = upstream
        report['model_calls'] = sum(row['kind'] == 'http' and row['path'].endswith('/chat/completions') for row in upstream)
        result = json.dumps(report, ensure_ascii=False, indent=2)
        assert configuration['key'] not in result
        (api.root / 'live-result.json').write_text(result, encoding='utf8')

    def observation(index):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            for row in jsonl(api.root / 'upstream-lanes.jsonl'):
                if row['index'] == index:
                    return row
            time.sleep(.08)
        raise AssertionError(f'No real upstream lane receipt for request {index}')

    def stream(client, label, path, body, *, disconnect=False):
        frames = []
        first_reasoning = None
        try:
            with client.stream('POST', path, json=body) as response:
                response.raise_for_status()
                assert 'text/event-stream' in response.headers['content-type']
                for line in response.iter_lines():
                    if not line.startswith('data:'):
                        continue
                    frame = json.loads(line[5:]); frames.append(frame)
                    if len(frames) > 14000:
                        raise ValueError('Live downstream frame limit exceeded')
                    if first_reasoning is None and frame['type'] == 'reasoning.delta' and frame.get('text'):
                        first_reasoning = time.monotonic_ns()
                    if disconnect and frame['type'] == 'text.delta' and frame.get('text'):
                        break  # Actually close the live HTTP stream mid-response.
        finally:
            # Keep a separate wire receipt, with reasoning/body represented by digest.
            wire = [{'type': frame.get('type'), 'run_id': frame.get('run_id'), 'seq': frame.get('seq'),
                     **({'sha256': digest(frame['text'])} if isinstance(frame.get('text'), str) else {})}
                    for frame in frames]
            (api.root / f'{label}-wire.json').write_text(json.dumps(wire, indent=2), encoding='utf8')
            save()
        failures = [frame['type'] for frame in frames if frame['type'] in {'stream.error', 'request.failed', 'request.timed_out'}]
        assert not failures, (label, failures)
        return frames, first_reasoning

    def ensure_idle(cid, run_id):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = api.call('GET', f'/fixture/stream-owner/{cid}/{run_id}')
            if state == {'phase':'idle', 'registered':False, 'sequence_tracked':False}:
                return state
            time.sleep(.12)
        raise AssertionError(('Real stream owner or sequence was not released', state))

    try:
        with api:
            case = api.create_case('F52 real stream boundaries')
            marker = 'STREAM_PROOF_' + uuid.uuid4().hex
            original = OLD + '\n' + marker + '\n'
            api.put(FILE, original)
            profile = api.call('POST', '/provider-profiles', {'provider':'deepseek', 'name':'F52 live AgentHost',
                'base_url':configuration['base_url'], 'default_model':configuration['model'], 'api_key':configuration['key']})
            model = {'provider_profile_id':profile['id'], 'model':configuration['model'], 'reasoning_effort':'low'}

            def conversation(title, permission):
                return api.call('POST', '/conversations', {'case_id':case['id'], 'title':title,
                    'provider_profile_id':profile['id'], 'model':configuration['model'], 'permission_mode':permission})['id']

            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                              timeout=120, trust_env=False) as client:
                if phase == 'all':
                    cid = conversation('Reasoning and two real tools', 'approve_writes')
                    prompt = (f'隔离算例里的无害文本验收。先调用一次 read_text_file 读取 {FILE}。'
                              f'等实际工具结果回来后，才调用一次 propose_text_replacement，把 {OLD} 精确替换为 {NEW}。'
                              '保留其余文件内容，等待人工批准。实际批准后用一句话确认，并原样写出文件第2行验证串。'
                              '只用这两个工具各一次，批准后不再调用工具，回复使用纯文本。')
                    pending, first_visible = stream(client, 'pending', f'/conversations/{cid}/messages', {'content':prompt, **model})
                    pending_id = ordered(pending)
                    assert pending[0]['type'] == 'ready' and pending[-1]['type'] == 'stream.done'
                    read_index, = [i for i,row in enumerate(pending) if row['type'] == 'tool.result'
                                   and row.get('tool') == 'read_text_file']
                    assert pending[read_index]['ok'] and marker in pending[read_index]['result']
                    request_index, = [i for i,row in enumerate(pending) if row['type'] == 'approval.requested']
                    assert read_index < request_index
                    proposal = pending[request_index]['tool_call']
                    assert proposal['tool'] == 'propose_text_replacement'
                    assert proposal['args']['old_text'] == OLD and proposal['args']['new_text'] == NEW
                    assert proposal['args']['baseline_sha256'] == digest(original)
                    observed_first, observed_second = observation(1), observation(2)
                    first = compare(observed_first, pending[:read_index])
                    second = compare(observed_second, pending[read_index + 1:request_index])
                    assert any(row['type'] == 'reasoning.delta' for row in first['lanes'])
                    assert any(row['type'] == 'tool.delta' for row in first['lanes'] + second['lanes'])
                    pause = observed_first['pause']
                    assert pause['forwarded_at_ns'] <= first_visible < pause['next_allowed_at_ns'], (pause, first_visible)
                    state = ensure_idle(cid, pending_id)
                    report['pending'] = {'run_id':pending_id, 'frames':len(pending), 'checked_provider_requests':[first, second],
                        'reasoning_first_visible_ms':round((first_visible - pause['forwarded_at_ns'])/1e6, 2),
                        'actual_gap_ms':round((pause['next_allowed_at_ns'] - pause['forwarded_at_ns'])/1e6, 2),
                        'actual_tool_result_order':[read_index + 1, request_index + 1], 'owner_after':state}
                    save()

                    approved, _ = stream(client, 'approved', f'/conversations/{cid}/tool-calls/{proposal["id"]}:approve',
                                         {'reasoning_effort':'low'})
                    approved_id = ordered(approved)
                    assert approved_id != pending_id
                    assert [frame['type'] for frame in approved[:2]] == ['tool.result','ready']
                    assert approved[0]['approved'] and approved[0]['ok'] and approved[-1]['type'] == 'run.completed'
                    third = compare(observation(3), approved[2:])
                    state = ensure_idle(cid, approved_id)
                    assert first['combined_frames'] + second['combined_frames'] + third['combined_frames'] > 0
                    answer = ''.join(frame['text'] for frame in approved if frame['type'] == 'text.delta')
                    reasoning = ''.join(frame['text'] for frame in approved if frame['type'] == 'reasoning.delta')
                    assert marker in answer
                    history = api.call('GET', f'/conversations/{cid}')
                    saved, = [row['payload'] for row in history['events'] if row['type']=='message'
                              and row['payload'].get('role')=='assistant' and row['payload'].get('run_id')==approved_id]
                    assert saved['content'] == answer and saved.get('reasoning', '') == reasoning
                    content = api.call('GET', api.prefix + '/file?' + urlencode({'path':FILE}))['content']
                    assert content == original.replace(OLD, NEW)
                    report['approved'] = {'run_id':approved_id, 'frames':len(approved), 'first_two':['tool.result','ready'],
                        'stored_reasoning_sha256':digest(reasoning), 'stored_answer_sha256':digest(answer),
                        'provider_lanes':third, 'tool_write_verified':True, 'owner_after':state}
                    save()

                lost = conversation('Transport disconnect and original conversation retry', 'ask')
                partial, _ = stream(client, 'disconnect', f'/conversations/{lost}/messages', {'content':
                    '请认真列出一份包含 650 条的普通办公检查清单。每条编号，逐条用中文说明，直接开始。', **model},
                    disconnect=True)
                lost_id = ordered(partial)
                assert partial[-1]['type'] == 'text.delta'
                state = ensure_idle(lost, lost_id)
                raw = observation(budget - 1)
                assert not raw['done'] and raw['provider_completion_id']
                history = api.call('GET', f'/conversations/{lost}')
                terminal = [row['payload']['status'] for row in history['events'] if row['type']=='run_state'
                            and row['payload'].get('run_id')==lost_id][-1]
                assert terminal in {'cancelled','interrupted'}, terminal
                report['disconnect'] = {'conversation':lost, 'run_id':lost_id, 'wire_frames':len(partial),
                    'actual_upstream_done_received':False, 'durable_terminal':terminal, 'owner_after':state}
                save()
                resumed, _ = stream(client, 'resumed', f'/conversations/{lost}/messages', {'content':
                    '现在停止上一个列表。请用一句话确认本会话已继续，句中包含 RESUMED_F52。', **model})
                resumed_id = ordered(resumed)
                assert resumed_id != lost_id and resumed[-1]['type'] == 'run.completed'
                assert 'RESUMED_F52' in ''.join(frame['text'] for frame in resumed if frame['type']=='text.delta')
                report['resumed'] = {'conversation':lost, 'run_id':resumed_id, 'frames':len(resumed),
                    'provider_lanes':compare(observation(budget), resumed), 'owner_after':ensure_idle(lost, resumed_id)}
            with closing(sqlite3.connect((api.root / 'test.sqlite').as_uri()+'?mode=ro', uri=True)) as db:
                assert not db.execute("SELECT conversation_id FROM conversation_executions WHERE phase <> 'idle'").fetchall()
            save()

        identity = json.loads((api.root / 'api-birth.json').read_text(encoding='utf8'))
        deadline = time.monotonic() + 15
        while probe(identity)['state'] != 'exited' and time.monotonic() < deadline:
            time.sleep(.2)
        assert probe(identity)['state'] == 'exited'
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1', api.port)) != 0
        assert not (api.root / '.vault_key').exists()
        report['cleanup'] = {'api_pid':identity['pid'], 'api_exited':True, 'port_closed':True, 'vault_key_on_disk':False}
        save()
        assert report['model_calls'] == budget and all(row['status']==200 for row in report['upstream'] if row['kind']=='http')
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'detail':str(error).replace(configuration['key'],'[REDACTED]')[:950]}
        raise
    finally:
        save()
    print('LIVE_F52_PASS', api.root / 'live-result.json', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', required=True, type=int)
    parser.add_argument('--phase', choices=('all', 'disconnect'), default='all')
    args = parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)), args.phase)
