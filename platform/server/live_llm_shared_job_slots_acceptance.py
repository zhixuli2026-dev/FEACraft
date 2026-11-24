"""Real shared-workspace admission, cross-process cancellation and slot reuse."""
import asyncio
from copy import copy
import json
import uuid

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_publication_restart_acceptance import worker_peer


FIXTURE = '''
import asyncio
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Plan
from app.services import jobrunner

@app.post('/api/v1/testing/one-job-slot')
async def one_slot(user=Depends(get_current_user)):
    async with SessionLocal() as db:
        plan = (await db.scalars(select(Plan).where(Plan.code=='free'))).one()
        values = json.loads(plan.quotas)
        values['job_concurrent']=1
        plan.quotas=json.dumps(values)
        await db.commit()
    return {'limit':1}

original_summary = jobrunner._persist_summary
async def hold_before_commit(job_id):
    selected = root/'hold-before-commit'
    if selected.exists() and selected.read_text()==str(job_id):
        (root/'worker-draining').touch()
        async with asyncio.timeout(60):
            while not (root/'allow-settlement').exists(): await asyncio.sleep(.05)
    return await original_summary(job_id)
jobrunner._persist_summary=hold_before_commit
'''


def scenario(api, *, store=None, **_):
    workspace = api.call('GET','/workspaces')[0]['id']
    org = api.call('GET','/org/mine')['org']['id']
    email = uuid.uuid4().hex+'@example.com'
    member = copy(api)
    member.token = api.call('POST','/auth/register',{'email':email,'name':'Compute editor','password':'isolated-test-123'})['token']
    private = member.call('GET','/workspaces')[0]['id']
    def new_case(actor, workspace, name):
        result = actor.call('POST',f'/workspaces/{workspace}/cases',{'name':name})
        return '/cases/'+str(result['id'])
    member_case = new_case(member,private,'Editor private calculation')
    successor_case = new_case(api,workspace,'Owner second calculation')
    api.call('POST',f'/org/{org}/members',{'email':email,'role':'member'},expect=201)
    member.call('POST',f'/org/{org}/invitation:accept',{})
    api.call('PUT',f'/workspaces/{workspace}/sharing',{'org_id':org,'role':'member','enabled':True})
    api.call('POST','/testing/one-job-slot',{})
    entries = {}
    for label,actor,path in [('private',member,member_case),('shared',api,api.prefix),('next',api,successor_case)]:
        marker,release = api.root/(label+'-started'),api.root/(label+'-finish')
        script = f"""
from pathlib import Path
import time
with Path({str(marker)!r}).open('a') as f: f.write('once\\n')
deadline=time.monotonic()+120
while not Path({str(release)!r}).exists():
    if time.monotonic()>deadline: raise TimeoutError('Test command did not receive release')
    time.sleep(.05)
"""
        target = copy(actor)
        target.prefix = path
        put(target,'Ref/slot.py',script)
        entries[label]=(path,marker,release)
    request = {'runtime':'windows','command':'python Ref/slot.py','input_paths':['Ref/slot.py']}
    def submit(actor,label,*,expect=(200,202),key=None):
        return actor.call('POST',entries[label][0]+'/jobs',request,expect=expect,
                          extra_headers={'Idempotency-Key':key or uuid.uuid4().hex})
    def usage(actor):
        data = actor.call('GET','/org/mine')['usage']
        assert data['quotas']['job_concurrent']==1
        return {'running':data['running_jobs'],'occupied':data['occupied_job_slots']}
    result = {'backend':'s3' if store else 'fs','observations':{}}
    try:
        with worker_peer(api,store) as peer:
            independent = copy(peer)
            independent.token = member.token
            first = submit(independent,'private')['id']
            wait_for(entries['private'][1].exists,'Editor private command did not start')
            assert usage(member)=={'running':1,'occupied':1} and usage(api)=={'running':0,'occupied':0}
            # This editor already uses their own slot. The shared command bills its owner.
            shared = submit(member,'shared')['id']
            wait_for(entries['shared'][1].exists,'Shared command did not use the owner slot')
            assert usage(member)==usage(api)=={'running':1,'occupied':1}
            entries['private'][2].touch()
            independent.wait_job(first)
            assert usage(member)=={'running':0,'occupied':0}
            refused=submit(independent,'next',expect=429)
            assert '并发作业' in refused['detail']
            (api.root/'hold-before-commit').write_text(str(shared))
            assert peer.call('POST',f'/jobs/{shared}:cancel')['ok']
            wait_for((api.root/'worker-draining').exists,'The cancelled worker did not reach settlement')
            during=usage(peer)
            assert during=={'running':0,'occupied':1}
            result['observations']['cancelled_before_settlement']=during
            key=uuid.uuid4().hex
            next_job=submit(independent,'next',key=key)['id']
            duplicate=submit(member,'next',key=key)['id']
            assert duplicate==next_job
            pending=peer.call('GET',f'/jobs/{next_job}')
            assert pending['attempt']['phase']=='queued' and not entries['next'][1].exists()
            assert usage(api)==during
            (api.root/'allow-settlement').touch()
            wait_for(entries['next'][1].exists,'Queued successor failed to acquire the released slot',80)
            previous=peer.call('GET',f'/jobs/{shared}')
            assert previous['attempt']['phase']=='cancelled' and previous['attempt']['process_stop']['verified']
            assert usage(peer)=={'running':1,'occupied':1}
            entries['next'][2].touch()
            finished=peer.wait_job(next_job)
            assert finished['attempt']['id']==pending['attempt']['id'] and finished['attempt']['process_stop']['verified']
            assert all(marker.read_text().splitlines()==['once'] for _,marker,_ in entries.values())
            result['observations']['owner_after']=usage(peer)
            assert result['observations']['owner_after']==usage(independent)=={'running':0,'occupied':0}
            result.update(private_job=first,shared_job=shared,successor=next_job,
                          successor_attempt=finished['attempt']['id'],same_key_replay=duplicate,
                          billing_owner_over_limit_http=429,all_three_commands_executed_once=True)
    finally:
        (api.root/'allow-settlement').touch(exist_ok=True)
        for _,_,release in entries.values(): release.touch(exist_ok=True)
    (api.root/'shared-job-slots.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
