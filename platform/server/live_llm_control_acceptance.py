"""Cancel/disconnect actual DeepSeek streams, then resume the same conversation."""
import argparse
import asyncio
import json
import sys
import time

import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_approval_acceptance import S3_OBSERVE
from live_llm_edit_memory_acceptance import OBSERVE
from testsupport import IsolatedServer, SERVER

STATE = '''
from app.models import Conversation,ConversationExecution
from app.routers import agent
from app.routers.workspaces import get_owned_case
@app.get('/api/v1/fixture/control/{cid}')
async def control(cid:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  conv=await db.get(Conversation,cid)
  await get_owned_case(conv.case_id,user,db)
  row=await db.get(ConversationExecution,cid)
  return {'phase':row.phase if row else None,'registered':cid in agent._RUN_CANCELLATIONS}
'''


def verify(configuration, store, modes):
    api = IsolatedServer('live-deepseek-control', app='live_fixture:app')
    bootstrap = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n'
    (api.root / 'live_fixture.py').write_text(bootstrap + FIXTURE + S3_OBSERVE + OBSERVE + STATE, encoding='utf8')
    result = dict(passed=False, simulated_llm=False, model=configuration['model'], scenarios={})

    def save():
        trace = api.root / 'upstream.jsonl'
        result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        (api.root / 'live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')

    print('LIVE_CONTROL_EVIDENCE ' + str(api.root), flush=True)
    try:
        with api:
            case = api.create_case('Real stream control')
            profile = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': 'Real stream control',
                'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': configuration['key']})
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=180) as client:
                for mode in modes:
                    conv = api.call('POST', '/conversations', {'case_id': case['id'], 'title': mode,
                        'provider_profile_id': profile['id'], 'model': configuration['model'], 'permission_mode': 'ask'})
                    cid = conv['id']; events = []; stopped = False
                    record = result['scenarios'][mode] = {'conversation_id': cid}
                    prompt = ('请逐行写出从1到500的编号，每行再写一句关于有限体积法的说明。'
                              '不要省略，不要用代码或区间概括，直接开始输出。')
                    try:
                        with client.stream('POST', f'/conversations/{cid}/messages', json={
                            'content': prompt, 'provider_profile_id': profile['id'], 'model': configuration['model']}) as response:
                            response.raise_for_status()
                            for line in response.iter_lines():
                                if not line.startswith('data:'):
                                    continue
                                event = json.loads(line[5:]); events.append(event)
                                if event.get('type') == 'text.delta' and event.get('text') and not stopped:
                                    stopped = True
                                    record['received_real_text_before_stop'] = True
                                    if mode == 'disconnect':
                                        break  # Close the actual HTTP stream; no simulated provider error.
                                    cancel = client.post(f'/conversations/{cid}/messages:cancel')
                                    cancel.raise_for_status()
                                    assert cancel.json()['ok'], cancel.json()
                                    record['cancel_acknowledged'] = True
                    finally:
                        assert configuration['key'] not in json.dumps(events)
                        (api.root / (mode + '-events.json')).write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf8')
                        save()
                    assert stopped, [e.get('type') for e in events]
                    if mode == 'cancel':
                        assert any(e.get('type') == 'run.cancelled' for e in events), [e.get('type') for e in events]
                        assert not any(e.get('type') == 'run.completed' for e in events)
                    deadline = time.monotonic() + 30
                    while True:
                        state = api.call('GET', f'/fixture/control/{cid}')
                        record['last_state'] = state
                        if state['phase'] == 'idle' and not state['registered']:
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Actual interrupted run did not release its owner: ' + str(state))
                        time.sleep(.1)
                    history = api.call('GET', f'/conversations/{cid}')
                    states = [e['payload']['status'] for e in history['events'] if e['type'] == 'run_state']
                    assert states[-1] in {'cancelled', 'interrupted'}, states
                    record['terminal_status'] = states[-1]
                    resume = []
                    with client.stream('POST', f'/conversations/{cid}/messages', json={
                        'content': '停止之前的长输出。现在只回复：RESUMED_OK',
                        'provider_profile_id': profile['id'], 'model': configuration['model']}) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line.startswith('data:'):
                                resume.append(json.loads(line[5:]))
                    (api.root / (mode + '-resume-events.json')).write_text(json.dumps(resume, ensure_ascii=False, indent=2), encoding='utf8')
                    assert any(e.get('type') == 'run.completed' for e in resume), [e.get('type') for e in resume]
                    assert 'RESUMED_OK' in ''.join(e.get('text', '') for e in resume if e.get('type') == 'text.delta')
                    record.update(resumed_with_real_model=True, passed=True)
                    save(); print('LIVE_CONTROL_PASS ' + mode, flush=True)
                credits = api.call('GET', '/fixture/storage-credit')
                assert credits and all(row['state'] == 'settled' for row in credits)
                result.update(passed=True, all_credit_settled=True); save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1400]}
        save(); print('LIVE_CONTROL_FAILED ' + json.dumps(result['failure'], ensure_ascii=False), flush=True)
        return 1
    print('LIVE_CONTROL_OK ' + str(api.root / 'live-result.json'), flush=True)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--modes', default='cancel,disconnect')
    args = parser.parse_args(); modes = args.modes.split(',')
    if not modes or any(m not in {'cancel', 'disconnect'} for m in modes) or len(set(modes)) != len(modes):
        parser.error('Choose unique cancel/disconnect modes')
    configuration = asyncio.run(source(args.source_profile_id))
    from s3testsupport import ObjectStore
    with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:
        code = verify(configuration, store, modes)
    sys.exit(code)
