"""Exercise checkpoint deadlines and browser recovery with a live DeepSeek connection."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from live_llm_agent_recovery_acceptance import peer, stream
from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main


FIXTURE = r'''
import asyncio, hashlib, json, os, time, uuid
from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.deps import get_current_user
from app.models import Conversation, AgentEvent
from app.routers import agent
from app.routers.workspaces import get_owned_case
from app.services.durable_events import lock_case_execution

def stamp(name, value):
    dest = root / name
    temp = dest.with_suffix('.' + uuid.uuid4().hex)
    temp.write_text(json.dumps(value), encoding='utf8')
    temp.replace(dest)

def signature(content, reasoning):
    return [hashlib.sha256(v.encode()).hexdigest() for v in (content, reasoning)]

# Buffer lines arriving on this single genuine upstream connection to exercise a burst.
real_lines = httpx.Response.aiter_lines
async def network_lines(response, *args, **kwargs):
    if response.request.url.host == 'api.deepseek.com' and (root/'burst-input').exists():
        lines = []
        async for line in real_lines(response, *args, **kwargs):
            lines.append(line)
        for line in lines:
            yield line
    else:
        close_early = response.request.url.host == 'api.deepseek.com' and (root/'close-without-finish').exists()
        content_bytes = 0
        async for line in real_lines(response, *args, **kwargs):
            yield line
            if close_early and line.startswith('data:'):
                data=line[5:].strip()
                if data=='[DONE]':
                    raise AssertionError('Stream finished before controlled disconnect')
                choice=(json.loads(data).get('choices') or [{}])[0]
                assert not choice.get('finish_reason')
                content_bytes+=len((choice.get('delta',{}).get('content') or '').encode())
                if content_bytes>=80:
                    await response.aclose()
                    stamp('closed-response.json',{'status':response.status_code,'raw_content_bytes':content_bytes})
                    return
httpx.Response.aiter_lines = network_lines

upstream_send = httpx.AsyncClient.send
async def deadline_after_headers(client, request, *args, **kwargs):
    response = await upstream_send(client, request, *args, **kwargs)
    if request.url.host=='api.deepseek.com' and response.status_code==200:
        try:
            if (root/'socket-deadline').exists():
                request.extensions['timeout']['read'] = .001
                stamp('read-deadline-headers.json',{'status':response.status_code,'read_timeout_ms':1})
            if (root/'cancel-at-headers').exists():
                stamp('cancel-headers.json',{'status':response.status_code})
                deadline=time.monotonic()+45
                while (root/'cancel-at-headers').exists():
                    if time.monotonic()>deadline:
                        raise TimeoutError('Actual cancellation did not arrive')
                    await asyncio.sleep(.03)
        except BaseException:
            await response.aclose()
            raise
    return response
httpx.AsyncClient.send = deadline_after_headers

real_stream = agent.stream_chat_auto
trace = {}
async def observe_stream(**kwargs):
    mode = next((m for m in ('burst', 'stall') if (root/(m+'-input')).exists()), None)
    if mode:
        trace.clear()
        trace.update(mode=mode,total=0,committed=0,content=0,saved=None,previous={})
    original = real_stream(**kwargs)
    try:
        async for event in original:
            kind = event.get('type')
            if mode and kind in ('text.delta','reasoning.delta'):
                n = len(event['text'].encode())
                if n:
                    trace['previous'][trace['total']+n] = trace['total']
                trace['total'] += n
                if kind == 'text.delta':
                    trace['content'] += n
            yield event
            if (mode == 'stall' and trace['saved'] is not None and trace['content'] >= 40
                    and trace['total'] > trace['committed'] and time.monotonic()-trace['saved'] < .55
                    and not (root/'stalled.json').exists()):
                stamp('stalled.json', {'total':trace['total'],'saved':trace['saved'],'pid':os.getpid(),
                                       'paused':time.monotonic()})
                end = time.monotonic()+100
                while not (root/'release-stall').exists():
                    if time.monotonic()>end:
                        raise TimeoutError('Actual connection hold deadline')
                    await asyncio.sleep(.025)
    finally:
        await original.aclose()
agent.stream_chat_auto = observe_stream

real_append = agent._append_event
async def observe_checkpoint(db,cid,kind,payload):
    event_id = await real_append(db,cid,kind,payload)
    if kind=='run_checkpoint' and trace.get('mode') and (root/(trace['mode']+'-input')).exists():
        content, reasoning = payload.get('content',''),payload.get('reasoning','')
        size = len(content.encode())+len(reasoning.encode())
        # The pending read may be one delta ahead while the timer commits a prefix.
        assert size in trace['previous']
        db.info['checkpoint_window'] = dict(cid=cid,event_id=event_id,run=payload['run_id'],total=size,
            byte_delta=size-trace['committed'],delta_before_last_event=trace['previous'][size],
            prior_commit=trace['saved'],observed=time.monotonic(),hashes=signature(content,reasoning),
            content_bytes=len(content.encode()),reasoning_bytes=len(reasoning.encode()))
    return event_id
agent._append_event = observe_checkpoint

database_commit = AsyncSession.commit
async def observe_commit(db):
    saved = db.info.pop('checkpoint_window',None)
    result = await database_commit(db)
    if saved:
        saved['committed'] = time.monotonic()
        trace.update(committed=saved['total'],saved=saved['committed'])
        with (root/(trace['mode']+'-checkpoints.jsonl')).open('a',encoding='utf8') as f:
            f.write(json.dumps(saved)+'\n')
        if trace['mode']=='burst' and not (root/'lost-ack.json').exists():
            stamp('lost-ack.json',{'event_id':saved['event_id'],'total':saved['total']})
            await db.connection()
            raise ConnectionError('Controlled acknowledgement loss after real SQL commit')
    return result
AsyncSession.commit = observe_commit

@app.post('/api/v1/testing/checkpoint/{cid}/gap')
async def history_gap(cid:int,user=Depends(get_current_user),db=Depends(get_db)):
    conv=await db.get(Conversation,cid)
    await get_owned_case(conv.case_id,user,db,write=True)
    await lock_case_execution(db,conv.case_id)
    last=await db.scalar(select(func.max(AgentEvent.seq)).where(AgentEvent.conversation_id==cid))
    for i in range(320):
        db.add(AgentEvent(conversation_id=cid,seq=last+i+1,event_id=uuid.uuid4().hex,type='system',
                          payload='{"kind":"pagination_diagnostic"}'))
    await db.commit()
    return {'inserted':320}
'''


def checkpoints(api, mode):
    path = api.root / (mode+'-checkpoints.jsonl')
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []


def terminal_scenario(config, api, *, store=None, **_):
    api.create_case('Checkpoint terminal states')
    profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'Authorized DeepSeek endpoint',
                     'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
    cid=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Real terminal states','permission_mode':'ask',
                 'provider_profile_id':profile['id'],'model':config['model']})['id']
    route=f'/conversations/{cid}'
    def send(label,model):
        return stream(api,route+'/messages',{'content':'请写三个段落解释有限体积法和闭合边界的通量。',
                      'model':model,'provider_profile_id':profile['id']},label,config)
    def check(label,events,frame, *, partial=False):
        assert any(e['type']==frame for e in events), [e['type'] for e in events]
        history=api.call('GET',route)['events']
        run=next(e['payload']['run_id'] for e in reversed(history) if e['type']=='run_state')
        for kind in ('run_state','request_state'):
            states=[e['payload']['status'] for e in history if e['type']==kind and e['payload'].get('run_id')==run]
            assert states==['started',label],(kind,states)
        recovered=[e['recovered_checkpoint'] for e in history if e.get('recovered_checkpoint') and e['payload'].get('run_id')==run]
        if partial:
            expected={k:''.join(e['text'] for e in events if e['type']==t) for k,t in
                      [('content','text.delta'),('reasoning','reasoning.delta')]}
            assert expected['content'] and recovered==[{k:v for k,v in expected.items() if v}]
        else:
            assert not recovered
        return run
    failed=check('failed',send('provider-rejected','vf-unregistered-checkpoint-test-model'),'request.failed')
    (api.root/'socket-deadline').touch()
    try:
        timed_out=check('timed_out',send('read-timeout',config['model']),'request.timed_out')
        assert (api.root/'read-deadline-headers.json').is_file()
    finally:
        (api.root/'socket-deadline').unlink(missing_ok=True)
    flag=api.root/'cancel-at-headers'
    flag.touch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        task=pool.submit(send,'cancel',config['model'])
        try:
            def received_headers():
                if task.done():
                    task.result()
                    raise AssertionError('Stream ended before cancellation')
                return (api.root/'cancel-headers.json').is_file()
            wait_for(received_headers,'No upstream cancellation boundary',40)
            api.call('POST',route+'/messages:cancel')
            cancelled=check('cancelled',task.result(timeout=20),'run.cancelled')
        finally:
            flag.unlink(missing_ok=True)
    flag=api.root/'close-without-finish'
    flag.touch()
    try:
        partial_events=send('closed-before-finish',config['model'])
        assert any(e.get('error_type')=='upstream_incomplete' for e in partial_events)
        assert not any(e['type'] in ('run.completed','approval.requested','tool.result') for e in partial_events)
        partial_run=check('failed',partial_events,'stream.error',partial=True)
        assert (api.root/'closed-response.json').is_file()
    finally:
        flag.unlink(missing_ok=True)
    completed=check('completed',send('resumed',config['model']),'run.completed')
    before=api.call('GET',route)['events']
    with peer(api,store,'terminal-peer'):
        assert api.call('GET',route)['events']==before
    checkpoint=next(e['recovered_checkpoint'] for e in before if e['type']=='run_state' and e['payload']['run_id']==partial_run
                    and e['payload']['status']=='failed')
    settings={'base':api.base,'case':'Checkpoint terminal states','cid':cid,'token':api.token,
              'partial':{'content':'','reasoning':'',**checkpoint},'run':partial_run,'failed':True,
              'output':str(api.root/'failed-checkpoint-browser.json')}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).with_name('checkpoint_window_browser.py'))],
                   input=json.dumps(settings),text=True,check=True,timeout=120)
    report={'provider':'custom','actual_service':'api.deepseek.com','failed':failed,'timed_out':timed_out,
            'cancelled':cancelled,'completed':completed,'real_read_timeout_ms':1,'incomplete_stream':partial_run,
            'failed_partial_visible_after_restart':True,'browser':json.loads(Path(settings['output']).read_text())}
    api.create_case('Independent terminal-state readback')
    api.call('GET',route,expect=404)
    (api.root/'checkpoint-terminals.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


def scenario(config, api, *, store=None, **_):
    name = 'Checkpoint window recovery'
    api.create_case(name)
    profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Checkpoint delivery',
                       'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
    def conversation(title):
        return api.call('POST','/conversations',{'case_id':api.case_id,'title':title,'permission_mode':'ask',
                        'provider_profile_id':profile['id'],'model':config['model']})['id']
    def send(server, cid, prompt, label, effort='off'):
        return stream(server, f'/conversations/{cid}/messages', {'content':prompt,'provider_profile_id':profile['id'],
                      'model':config['model'],'reasoning_effort':effort}, label, config)

    burst = conversation('Actual upstream burst')
    (api.root/'burst-input').touch()
    try:
        events = send(api, burst, 'Write exactly 200 separate numbered rows of plain English text. On every row '
                      'write the full sentence “The independent mesh refinement calculation preserves the total '
                      'energy distribution throughout the entire reference volume.” Start each line with its row '
                      'number. Write all 200 lines in full without tables, explanations, omissions or tools.', 'burst')
    finally:
        (api.root/'burst-input').unlink(missing_ok=True)
    assert any(e['type']=='run.completed' for e in events)
    records = checkpoints(api,'burst')
    crossings = [r for r in records if r['prior_commit'] is not None and r['byte_delta']>=16384
                 and r['observed']-r['prior_commit']<.75]
    assert crossings, [(r['byte_delta'],r['observed']-(r['prior_commit'] or r['observed'])) for r in records]
    assert all(r['delta_before_last_event']-(r['total']-r['byte_delta'])<16384 for r in crossings)
    assert all(r['byte_delta']>0 for r in records)
    tail=records[-1]
    assert 0<tail['byte_delta']<16384 and tail['observed']-tail['prior_commit']<.75
    ack=json.loads((api.root/'lost-ack.json').read_text())
    history=api.call('GET',f'/conversations/{burst}')['events']
    snapshots=[e for e in history if e['type']=='run_checkpoint']
    assert len(snapshots)==len(records) and len({e['seq'] for e in snapshots})==len(records)
    assert [e['id'] for e in snapshots]==[r['event_id'] for r in records] and ack['event_id']==snapshots[0]['id']
    def text(kind):
        return ''.join(e['text'] for e in events if e['type']==kind)
    assert records[-1]['hashes']==[hashlib.sha256(text(kind).encode()).hexdigest()
                                    for kind in ('text.delta','reasoning.delta')]

    cid = conversation('Interrupted real stream')
    route = f'/conversations/{cid}'
    source = peer(api, store, 'checkpoint-window-source')
    with (source.root/'agent_peer_fixture.py').open('a',encoding='utf8') as fixture:
        fixture.write('\n'+FIXTURE)
    (api.root/'stall-input').touch()
    try:
        with source:
            source.token=api.token
            with ThreadPoolExecutor(max_workers=1) as pool:
                task=pool.submit(send,source,cid,'请用普通纯文本、三个段落讲解闭合控制体体积积分与面通量的关系。'
                                 '给出推导和一个数值例子，约800字，不用任何工具，不使用 Markdown。','stall','high')
                try:
                    def pause_seen():
                        if task.done():
                            observed = task.result()
                            statuses = [e['payload'].get('status') for e in api.call('GET',route)['events']
                                        if e['type'] in ('run_state','request_state')]
                            raise AssertionError(f'Actual stream ended before pause: {len(observed)} frames, {statuses}')
                        return (api.root/'stalled.json').is_file()
                    wait_for(pause_seen,'No real content pause',90)
                    paused=json.loads((api.root/'stalled.json').read_text())
                    saved=wait_for(lambda:next((r for r in checkpoints(api,'stall') if r['total']==paused['total']),None),
                                   'No persisted checkpoint during silence',8)
                    elapsed=saved['observed']-paused['saved']
                    assert .73<=elapsed<1.75 and saved['byte_delta']<16384 and saved['observed']>=paused['paused'],elapsed
                    time.sleep(.85)
                    assert sum(r['total']==paused['total'] for r in checkpoints(api,'stall'))==1
                    original=api.call('GET',route)['events']
                    checkpoint=next(e for e in original if e['id']==saved['event_id'])
                    assert checkpoint['type']=='run_checkpoint'
                    partial={k:checkpoint['payload'].get(k,'') for k in ('content','reasoning')}
                    assert partial['content'] and saved['hashes']==[hashlib.sha256(v.encode()).hexdigest() for v in partial.values()]
                    api.call('POST',f'/testing/checkpoint/{cid}/gap',{})
                    kill_owned_api(source,paused['pid'])
                    try:
                        task.result(timeout=15)
                    except Exception as failure:
                        disconnect=type(failure).__name__
                    else:
                        raise AssertionError('Terminated live stream returned normally')
                finally:
                    (api.root/'release-stall').touch()
    finally:
        (api.root/'stall-input').unlink(missing_ok=True)

    with peer(api,store,'checkpoint-window-recovery'):
        assert api.call('GET',route+'/recovery')['phase']=='idle'
    finished=send(api,cid,'请直接回复“恢复后继续”。','completed')
    assert any(e['type']=='run.completed' for e in finished)
    detail=api.call('GET',route)
    interrupted=next(e for e in detail['events'] if e['type']=='run_state' and e['payload']['status']=='interrupted')
    assert interrupted['recovered_checkpoint']=={k:v for k,v in partial.items() if v}
    assert detail['event_window']['has_more'] and not any(e['id']==saved['event_id'] for e in detail['events'])
    assert any(e['id']==saved['event_id'] for e in api.call('GET',route+'?offset=280&limit=280&snapshot_id='+
                                                             str(detail['event_window']['snapshot_id']))['events'])
    api.call('POST',route+'/fork',{'head_event_id':saved['event_id']},expect=409)
    settings={'base':api.base,'case':name,'cid':cid,'token':api.token,'partial':partial,'run':saved['run'],
              'output':str(api.root/'checkpoint-browser.json')}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).with_name('checkpoint_window_browser.py'))],
                   input=json.dumps(settings),text=True,check=True,timeout=150)
    report={'passed':True,'byte_crossings':crossings,'final_checkpoint_bytes':tail['total'],
            'terminal_flush_bytes':tail['byte_delta'],
            'lost_acknowledgement_event_id':ack['event_id'],'unique_committed_checkpoints':len(snapshots),
            'stall_seconds_since_previous_commit':elapsed,'no_duplicate_while_idle':True,'disconnect':disconnect,
            'cross_page_partial_hashes':saved['hashes'],'content_bytes':saved['content_bytes'],
            'reasoning_bytes':saved['reasoning_bytes'],'browser':json.loads(Path(settings['output']).read_text())}
    (api.root/'checkpoint-window.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent checkpoint proof readback')
    return report


if __name__=='__main__':
    bundle=Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'index.html').is_file()
    mount="\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/',StaticFiles(directory="+repr(str(bundle))+",html=True))\n"
    selected=terminal_scenario if os.environ.get('VIBEFLOW_CHECKPOINT_TERMINALS')=='1' else scenario
    asyncio.run(main(selected_factory=lambda config:partial(selected,config),fixture_override=FIXTURE+mount))
