"""Exercise all five real event topics, account isolation and cross-API replay."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import time
import uuid

import aiohttp

from live_llm_job_repeat_acceptance import main, put, write
from live_llm_publication_restart_acceptance import worker_peer
from viz_execution_smoke import STL


FIXTURE = '''
import os
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import Plan
from app.services import eventbus

os.environ['VIBEFLOW_WSL_DISTRO'] = 'vf-events-unavailable-' + str(os.getpid())

@app.get('/api/v1/testing/event-subscribers')
async def subscribers(user=Depends(get_current_user)):
    return {'count':len(eventbus._SUBSCRIBERS.get(user.id,()))}

@app.post('/api/v1/testing/event-job-limit/{limit}')
async def limit_jobs(limit:int,user=Depends(get_current_user),db=Depends(get_db)):
    assert limit in {0,4}
    plan = (await db.scalars(select(Plan).where(Plan.code=='free'))).one()
    values = json.loads(plan.quotas)
    values['job_concurrent'] = limit
    plan.quotas = json.dumps(values)
    await db.commit()
    return {'limit':limit}
'''


@asynccontextmanager
async def buffered(socket):
    messages = asyncio.Queue()

    async def collect():
        try:
            while True:
                frame = await socket.receive()
                await messages.put(frame)
                if frame.type!=aiohttp.WSMsgType.TEXT:
                    return
        except Exception as error:
            await messages.put(error)

    async with socket:
        reader = asyncio.create_task(collect())
        try:
            yield messages
        finally:
            reader.cancel()
            await asyncio.gather(reader,return_exceptions=True)


async def next_frame(messages, seconds):
    frame = await asyncio.wait_for(messages.get(),timeout=seconds)
    if isinstance(frame, Exception):
        raise frame
    return frame


async def receive(socket, topic, predicate=lambda _:True, *, seconds=12):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            frame = await next_frame(socket,max(.01,end-time.monotonic()))
        except asyncio.TimeoutError:
            break
        if frame.type!=aiohttp.WSMsgType.TEXT:
            raise AssertionError(f'WebSocket closed while waiting for {topic}: {frame.type.name}')
        event = json.loads(frame.data)
        if event.get('type')==topic and predicate(event):
            return event
    raise TimeoutError('Did not receive '+topic)


async def assert_no_topics(socket, topics, *, own_case, seconds=.8):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            frame = await next_frame(socket,max(.01,end-time.monotonic()))
        except asyncio.TimeoutError:
            return
        assert frame.type==aiohttp.WSMsgType.TEXT, frame.type.name
        event = json.loads(frame.data)
        if event.get('source') == 'file_watch' and event.get('initial') and event.get('case_id') == own_case:
            continue
        assert event.get('type') not in topics


def scenario(api, *, store=None, **_):
    prefix = api.prefix
    workspace = api.call('GET','/workspaces')[0]['id']
    secondary = api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Second case of same user'})['id']
    other_email = uuid.uuid4().hex+'@example.com'
    foreign = api.call('POST','/auth/register',{'email':other_email,
        'password':'event-topic-other-password','name':'Other event account'})
    other_headers = {'Authorization':'Bearer '+foreign['token']}
    other_workspace = api.call('GET','/workspaces',extra_headers=other_headers)[0]['id']
    third = api.call('POST',f'/workspaces/{other_workspace}/cases',{'name':'Foreign event case'},extra_headers=other_headers)['id']
    put(api,'D-post/topic-surface.stl',STL)
    report = {'storage':'s3' if store else 'fs','events':{}}
    path = api.root/'event-topics.json'

    def save():
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')

    def submit(server, body, expected):
        headers = {'Idempotency-Key':uuid.uuid4().hex}
        end = time.monotonic()+45
        while True:
            response = server.call('POST',prefix+'/jobs',body,expect=(expected,409),extra_headers=headers)
            detail = response.get('detail')
            if not isinstance(detail,dict) or detail.get('code') not in {'storage_quota_busy','validation_busy'}:
                return response
            if time.monotonic()>=end:
                raise TimeoutError('Actual job admission stayed busy')
            time.sleep(.1)

    def start_scene():
        route = prefix+'/viz:sessions'
        end = time.monotonic()+45
        while True:
            response = api.call('POST',route,{'file_path':'D-post/topic-surface.stl'},expect=(200,409))
            if 'id' in response:
                return response
            detail = response.get('detail')
            assert isinstance(detail,dict) and detail['code'] in {'storage_quota_busy','validation_busy'}, detail
            failed = api.call('GET',route+'?include_closed=true')
            assert all(row['phase'] in {'failed','closed'} and
                       (row['process'] is None or row['cleanup_verified'] is True) for row in failed), failed
            if time.monotonic()>=end:
                raise TimeoutError('Actual visualization admission stayed busy')
            time.sleep(.15)

    async def verify(peer):
        base = api.base+'/ws/events'
        async with aiohttp.ClientSession(trust_env=False) as client:
            async def connect(case_id, token, after=None):
                params = {'case_id':case_id,'token':token}
                if after is not None:
                    params['after'] = after
                return await client.ws_connect(base,params=params)

            try:
                unexpected = await connect(api.case_id,foreign['token'])
            except aiohttp.WSServerHandshakeError as error:
                assert error.status==403
            else:
                await unexpected.close()
                raise AssertionError('Other account entered the protected case event stream')
            async with AsyncExitStack() as sockets:
                first = await sockets.enter_async_context(buffered(await connect(api.case_id,api.token)))
                same = await sockets.enter_async_context(buffered(await connect(secondary,api.token)))
                other = await sockets.enter_async_context(buffered(await connect(third,foreign['token'])))
                for socket, case_id in ((first,api.case_id),(same,secondary),(other,third)):
                    assert (await receive(socket,'ready'))['case_id']==case_id
                assert (await asyncio.to_thread(api.call,'GET','/testing/event-subscribers'))['count']==2

                file_path = 'Ref/event-topics-cross-api.txt'
                await asyncio.to_thread(write,peer,'PUT',prefix+'/file',{'path':file_path,'content':'Cross-API file'})
                file_event = await receive(first,'files.changed',lambda event:file_path in event.get('paths',[]) and event.get('event_id'))
                report['events']['files.changed'] = file_event
                submitted = await asyncio.to_thread(submit,peer,
                    {'type':'shell','runtime':'windows','command':'echo EVENT_TOPICS_JOB','timeout_seconds':90},200)
                running = await receive(first,'jobs.updated',lambda event:event.get('job_id')==submitted['id'] and event['status']=='running')
                finished, completed = await asyncio.gather(
                    asyncio.to_thread(peer.wait_job,submitted['id'],90),
                    receive(first,'jobs.updated',lambda event:event.get('job_id')==submitted['id'] and event['status']=='succeeded',seconds=90))
                assert finished['exit_code']==0 and file_event['seq']<running['seq']<completed['seq']
                report['events']['jobs.updated'] = [running,completed]
                async with buffered(await connect(api.case_id,api.token,after=running['seq'])) as resumed:
                    assert (await receive(resumed,'ready'))['durable_cursor']==running['seq']
                    repeated = await receive(resumed,'jobs.updated',lambda event:event.get('job_id')==submitted['id'])
                    assert repeated==completed
                report['cross_api_durable_replay'] = True
                await assert_no_topics(same,{'jobs.updated','files.changed','scene.available'},own_case=secondary)

                await asyncio.to_thread(api.call,'POST','/testing/event-job-limit/0')
                try:
                    refused = await asyncio.to_thread(submit,api,{'type':'shell','command':'echo UNREACHABLE'},429)
                    assert '并发作业' in refused['detail']
                    report['quota_http'] = 429
                    save()
                    limit, same_limit = await asyncio.gather(receive(first,'quota.warning'),receive(same,'quota.warning'))
                    assert limit==same_limit and 'case_id' not in limit and 'event_id' not in limit
                    assert limit['dimension']=='并发作业' and limit['limit']==0
                    report['events']['quota.warning'] = limit
                    save()
                finally:
                    await asyncio.to_thread(api.call,'POST','/testing/event-job-limit/4')

                async with client.get(api.base+'/api/v1/runtime/capabilities',
                    headers={'Authorization':'Bearer '+api.token},timeout=aiohttp.ClientTimeout(total=100)) as response:
                    assert response.status==200, await response.text()
                    capabilities = await response.json()
                wsl = capabilities['wsl']
                assert not wsl['reachable'] and wsl['distro'].startswith('vf-events-unavailable-')
                runtime, same_runtime = await asyncio.gather(receive(first,'runtime.degraded'),receive(same,'runtime.degraded'))
                assert runtime==same_runtime and runtime['runtime']=='wsl' and 'case_id' not in runtime
                assert all(runtime[key] is bool(wsl.get(key)) for key in ('reachable','openfoam','wmake','gmsh'))
                report['events']['runtime.degraded'] = runtime
                save()

                scene = await asyncio.to_thread(start_scene)
                try:
                    created = await receive(first,'scene.available',lambda event:event.get('session_id')==scene['id'])
                    assert created['case_id']==api.case_id and created['status']=='created' and scene['alive']
                finally:
                    await asyncio.to_thread(api.call,'DELETE',f'/viz:sessions/{scene["id"]}')
                closed = await receive(first,'scene.available',lambda event:event.get('session_id')==scene['id'])
                assert closed['case_id']==api.case_id and closed['status']=='closed'
                status = await asyncio.to_thread(api.call,'GET',f'/viz:sessions/{scene["id"]}')
                assert status['cleanup_verified'] and status['phase']=='closed'
                report['events']['scene.available'] = [created,closed]

                await asyncio.gather(assert_no_topics(same,{'jobs.updated','scene.available','files.changed'},own_case=secondary),
                                     assert_no_topics(other,set(report['events']),own_case=third))
                report.update(foreign_case_connect_http=403,foreign_account_no_topics=True,same_account_case_isolated=True,
                    same_account_notices_received_on_both=True,renderer_cleanup_verified=True,requested_wsl_unavailable=True)
                organization = (await asyncio.to_thread(api.call,'GET','/org/mine'))['org']['id']
                await asyncio.to_thread(api.call,'POST',f'/org/{organization}/members',
                    {'email':other_email,'role':'viewer'},expect=201)
                await asyncio.to_thread(api.call,'POST',f'/org/{organization}/invitation:accept',{},extra_headers=other_headers)
                share = {'org_id':organization,'role':'viewer','enabled':True}
                sharing = f'/workspaces/{workspace}/sharing'
                await asyncio.to_thread(api.call,'PUT',sharing,share)
                async with buffered(await connect(api.case_id,foreign['token'])) as shared:
                    assert (await receive(shared,'ready'))['case_id']==api.case_id
                    await asyncio.to_thread(api.call,'PUT',sharing,{**share,'enabled':False})
                    frame = await next_frame(shared,10)
                    assert frame.type==aiohttp.WSMsgType.CLOSE and frame.data==4403
                    report['socket_closed_after_revoke'] = frame.data
                save()
            for _ in range(45):
                current = await asyncio.to_thread(api.call,'GET','/testing/event-subscribers')
                if current['count']==0:
                    break
                await asyncio.sleep(.1)
            assert current['count']==0
            report['subscribers_after_disconnect'] = current['count']
            save()

    with worker_peer(api,store) as peer:
        asyncio.run(verify(peer))
    return report


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=6))
