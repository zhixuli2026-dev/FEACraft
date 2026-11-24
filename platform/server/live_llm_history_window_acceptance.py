"""Measure production history reads on isolated stores and verify actual DeepSeek turns."""
import asyncio
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import uuid

import httpx

from live_llm_job_repeat_acceptance import main


FIXTURE = '''
from contextvars import ContextVar
import time, uuid
from fastapi import Depends, HTTPException
from sqlalchemy import event, func, insert, select, text
from app.db import get_db
from app.deps import get_current_user
from app.models import AgentEvent, Conversation
from app.routers import agent
from app.routers.workspaces import get_owned_case
from app.services.conversation_history import encoded, window_query

reading = ContextVar('history_read', default=None)
@event.listens_for(AgentEvent, 'load')
def counted(row, context):
    measurement = reading.get()
    if measurement is not None:
        measurement['orm_rows'] += 1

production_page = agent._history_page
async def measured_page(db, segments, limit, budget, **kwargs):
    report = dict(limit=limit, budget=budget, offset=kwargs.get('offset', 0),
                  cid=segments[-1][0], orm_rows=0)
    token = reading.set(report)
    start = time.monotonic()
    try:
        page = await production_page(db, segments, limit, budget, **kwargs)
        report.update(returned=len(page), elapsed_ms=round((time.monotonic()-start)*1000),
                      payload_bytes=sum(len(encoded(e['payload'])) for e in page))
        with (root/'window-reads.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps(report)+'\\n')
        return page
    finally:
        reading.reset(token)
agent._history_page = measured_page

async def owned(cid, user, db):
    conv = await db.get(Conversation, cid)
    if conv is None:
        raise HTTPException(404)
    await get_owned_case(conv.case_id, user, db)

@app.post('/api/v1/testing/windows/{cid}/seed/{recipe}')
async def seed_window(cid:int, recipe:str, user=Depends(get_current_user), db=Depends(get_db)):
    await owned(cid, user, db)
    sizes = {'scale':12100, 'notes':1300, 'user':320, 'append':1, 'pairs':3, 'huge':1}
    if recipe not in sizes:
        raise HTTPException(400)
    last = await db.scalar(select(func.max(AgentEvent.seq)).where(AgentEvent.conversation_id==cid)) or 0
    total = 0
    batch = []
    for n in range(1, sizes[recipe]+1):
        kind = 'system'
        if recipe in {'user', 'append'}:
            kind = 'message'
            payload = {'role':'user', 'content':f'HISTORY-{recipe}-{n:05}'}
        elif recipe == 'huge':
            payload = {'args':{'document':'x'*(17*1024*1024)}, 'kind':'note', 'reasoning':'界'*(6*1024*1024)}
        else:
            length = {'scale':2048, 'notes':0, 'pairs':2200000}[recipe]
            payload = {'kind':'note','text':f'{n}-'+'x'*length}
        data = encoded(payload)
        total += len(data)
        batch.append(dict(conversation_id=cid, seq=last+n, event_id=uuid.uuid4().hex, type=kind,
                          payload=data.decode('utf8')))
        if len(batch)==128 or n==sizes[recipe]:
            await db.execute(insert(AgentEvent), batch)
            batch.clear()
    await db.commit()
    return {'bytes':total, 'inserted':sizes[recipe]}

@app.get('/api/v1/testing/windows/{cid}/model-window')
async def model_window(cid:int, user=Depends(get_current_user), db=Depends(get_db)):
    await owned(cid, user, db)
    page = await agent._load_events(db, cid)
    return {'count':len(page), 'payload_bytes':sum(len(encoded(e['payload'])) for e in page),
            'truncated':sum(e['payload'].get('truncated') is True for e in page)}

@app.get('/api/v1/testing/windows/{cid}/order-plan')
async def order_plan(cid:int, user=Depends(get_current_user), db=Depends(get_db)):
    await owned(cid, user, db)
    dialect = db.get_bind().dialect
    prefix = 'EXPLAIN QUERY PLAN' if dialect.name=='sqlite' else 'EXPLAIN'
    async def explain(offset):
        statement = window_query(cid, None, 280, offset).compile(dialect=dialect, compile_kwargs={'literal_binds':True})
        rows = await db.execute(text(prefix+' '+str(statement)))
        return [str(tuple(row)) for row in rows]
    initial = await explain(5800)
    await db.execute(text('ANALYZE agent_events'))
    return {'initial':initial, 'deep_with_statistics':await explain(5800), 'latest_with_statistics':await explain(0)}
'''


def scenario(configuration, api, **_):
    name = 'History windows '+uuid.uuid4().hex[:8]
    api.create_case(name)
    profile = api.call('POST', '/provider-profiles', {
        'name':'History window acceptance', 'provider':'deepseek', 'base_url':configuration['base_url'],
        'default_model':configuration['model'], 'api_key':configuration['key']})

    def new(title):
        return api.call('POST', '/conversations', {'title':title, 'case_id':api.case_id,
                        'permission_mode':'ask', 'model':configuration['model'], 'provider_profile_id':profile['id']})['id']

    def seed(cid, recipe):
        return api.call('POST', f'/testing/windows/{cid}/seed/{recipe}')

    def get(cid, query=''):
        return api.call('GET', f'/conversations/{cid}'+query)

    def model_window(cid):
        return api.call('GET', f'/testing/windows/{cid}/model-window')

    def readings():
        path = api.root/'window-reads.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]

    scale = new('Scale')
    dataset = seed(scale, 'scale')
    first = get(scale)
    window = first['event_window']
    assert window['total']==12100 and window['next_offset']==280 and len(first['events'])==280
    deep = get(scale, '?offset=5800&limit=280')
    assert [e['seq'] for e in deep['events']]==list(range(6101,6301))
    assert not deep['event_window']['has_more'] and not get(scale, '?offset=6000')['events']
    archive = get(scale, '?limit=6000')
    assert len(archive['events'])==6000 and not archive['event_window']['has_more']
    assert archive['events'][0]['seq']==6101 and archive['events'][-1]['seq']==12100
    del archive
    assert model_window(scale)['count']==1200
    plan = api.call('GET', f'/testing/windows/{scale}/order-plan')
    assert any('ix_agent_events_history' in line for line in plan['latest_with_statistics']), plan
    fork = api.call('POST', f'/conversations/{scale}/fork')['id']
    seed(scale, 'append')
    seed(fork, 'append')
    branch = get(fork, '?limit=4')
    assert branch['event_window']['total']==12101
    assert [e['id'] for e in branch['events'][:-1]]==[e['id'] for e in first['events'][-3:]]
    assert branch['events'][-1]['id'] != get(scale, '?limit=1')['events'][0]['id']

    paired = new('Byte boundary')
    seed(paired, 'pairs')
    page = get(paired)
    pages = []
    while True:
        pages.append([e['seq'] for e in page['events']])
        w = page['event_window']
        if not w['has_more']:
            break
        page = get(paired, f"?offset={w['next_offset']}&snapshot_id={w['snapshot_id']}")
    assert pages==[[3],[2],[1]], pages
    large = new('One large structured record')
    raw_bytes = seed(large, 'huge')['bytes']
    for query, budget in [('',4), ('?limit=6000',16)]:
        payload = get(large, query)['events'][0]['payload']
        assert payload['kind']=='note' and payload['truncated'] and 'args' not in payload
        assert len(json.dumps(payload, ensure_ascii=False).encode())<=budget*1024**2
    clipped = model_window(large)
    assert clipped['truncated']==1 and 7*1024**2 < clipped['payload_bytes'] <= 8*1024**2

    cid = new('Browser and real model')
    seed(cid, 'notes')
    seed(cid, 'user')
    marker = 'LATEST_'+uuid.uuid4().hex[:16]
    with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization':'Bearer '+api.token},
                      timeout=180, trust_env=False) as client:
        response = client.post(f'/conversations/{cid}/messages', json={'content':'Reply with exactly '+marker,
                               'model':configuration['model'], 'provider_profile_id':profile['id']})
        response.raise_for_status()
        events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
        assert any(e['type']=='run.completed' for e in events)
    result = get(cid)
    answer = next(e['payload'] for e in result['events'] if e['type']=='message'
                  and e['payload'].get('role')=='assistant' and marker in e['payload'].get('content',''))
    run_id = answer['run_id']
    states = [e['payload'] for e in result['events'] if e['type']=='run_state' and e['payload'].get('run_id')==run_id]
    start = next(s for s in states if s['status']=='started')
    end = next(s for s in states if s['status']=='completed')
    assert end['started_at']==start['started_at'] and end['ended_at']>=start['started_at']
    assert end['duration_ms']==round((end['ended_at']-start['started_at'])*1000) and end['duration_ms']>0
    api.call('GET', f'/conversations/{cid}?offset=-1', expect=400)
    api.call('GET', f'/conversations/{cid}?limit=6001', expect=400)
    api.call('GET', f'/conversations/{cid}?snapshot_id=-1', expect=422)
    stranger = api.call('POST', '/auth/register', {'email':uuid.uuid4().hex+'@example.com',
                        'name':'Other reader', 'password':'isolated-test-123'})
    api.call('GET', f"/conversations/{cid}?snapshot_id={result['event_window']['snapshot_id']}", expect=404,
             extra_headers={'Authorization':'Bearer '+stranger['token']})
    data = {'base':api.base, 'case':name, 'token':api.token, 'cid':cid, 'marker':marker,
            'run_id':run_id, 'output':str(api.root/'window-browser.json')}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], str(Path(__file__).with_name('history_window_browser.py'))],
                   input=json.dumps(data), text=True, check=True, timeout=160)
    metrics = readings()
    assert all(item['orm_rows']<=item['returned']+4 and item['payload_bytes']<=item['budget'] for item in metrics)
    assert any(item['cid']==cid and item['budget']==8*1024**2 and item['returned']==1200 for item in metrics)
    report = {'database':'postgresql' if api.database_url else 'sqlite', 'dataset':dataset, 'ordering':plan,
              'scale_reads':[m for m in metrics if m['cid']==scale], 'single_input_bytes':raw_bytes,
              'model_clipped':clipped, 'byte_boundary_pages':pages, 'branch_cutoff':True, 'other_owner_http':404,
              'real_run_id':run_id, 'duration_ms':end['duration_ms'], 'browser':json.loads(Path(data['output']).read_text())}
    (api.root/'history-window.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    return report


if __name__=='__main__':
    bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'index.html').is_file()
    mount = "\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/', StaticFiles(directory="+repr(str(bundle))+", html=True))\n"
    asyncio.run(main(selected_factory=lambda config:partial(scenario, config), fixture_override=FIXTURE+mount))
