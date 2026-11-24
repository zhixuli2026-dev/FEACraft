"""Two actual execution hosts contend for one account slot in shared PostgreSQL."""
import asyncio
from copy import copy
import json
from pathlib import Path
import time
import uuid

from arq import create_pool
from arq.connections import RedisSettings
import asyncpg

from app.services import process_identity_native
from app.services.job_execution_hosts import queue_name
from app.services.owned_process import wsl_path
import live_llm_remote_worker_acceptance as remote
import workflow_batch_redis_support as local


HOOK = '''
from app.services import job_execution,job_delivery
claim_slot = job_execution.claim_job
async def simultaneous_claim(job_id,spec):
    if (SLOTS/'armed').is_file() and not (SLOTS/'admit').exists():
        (SLOTS/(LABEL+'-request-'+str(job_id))).touch()
        async with asyncio.timeout(75):
            while not (SLOTS/'admit').is_file(): await asyncio.sleep(.04)
    return await claim_slot(job_id,spec)
job_execution.claim_job = simultaneous_claim
real_delivery = job_delivery.deliver
async def receive(job_id,**kwargs):
    result = await real_delivery(job_id,**kwargs)
    if (SLOTS/'armed').is_file():
        (SLOTS/(LABEL+'-'+str(job_id)+'-'+result)).touch()
    return result
job_delivery.deliver=receive
'''


def hook(root,windows):
    return ('SLOTS=Path('+repr(str(root) if windows else wsl_path(root))+')\nLABEL='
            +repr('windows' if windows else 'linux')+'\n'+HOOK)


class LinuxWorker(remote.LinuxWorker):
    def fixture_code(self,root):
        return hook(self.api.root,False)


async def until(read, description, seconds=80):
    deadline=time.monotonic()+seconds
    while True:
        value=await read()
        if value: return value
        if time.monotonic()>deadline: raise TimeoutError(description)
        await asyncio.sleep(.15)


async def race(api,worker,selected):
    assert selected.database.startswith('tmp_vf_remoteworker_')
    actor=copy(api)
    await asyncio.to_thread(actor.create_case,'One slot across two execution hosts')
    workspace=(await asyncio.to_thread(actor.call,'GET','/workspaces'))[0]['id']
    second=await asyncio.to_thread(actor.call,'POST',f'/workspaces/{workspace}/cases',{'name':'Other host'})
    cases={'windows':actor.prefix,'linux':'/cases/'+str(second['id'])}
    root=api.root
    windows_api=copy(api)
    windows_api.env=dict(api.env,VIBEFLOW_WORKER_HOST_QUEUE='true')
    windows=local.Workers(windows_api,worker.broker,worker.store)
    template=local.WORKER
    async def start_windows():
        try:
            local.WORKER=template.replace('asyncio.run(main())',hook(root,True)+'\nasyncio.run(main())').replace(
                'await worker.close();','await worker.close(grace_seconds=0);')
            await asyncio.to_thread(windows.start)
        finally:
            local.WORKER=template
    async def call(method,path,body=None,**kwargs):
        return await asyncio.to_thread(actor.call,method,path,body,**kwargs)
    async def files(pattern):
        return list(root.glob(pattern))
    async def state(identifier):
        return await call('GET',f'/jobs/{identifier}')
    async def slots():
        return (await call('GET','/org/mine'))['usage']['occupied_job_slots']
    targets={'windows':process_identity_native.host_id(),'linux':worker.birth['host_id']}
    assert targets['windows']!=targets['linux']
    result={'host_ids':targets}
    jobs={}
    db=await asyncpg.connect(host=selected.host,port=selected.port,user=selected.username,
                             password=selected.password,database=selected.database)
    pool=None
    try:
        original=await db.fetchval("SELECT quotas FROM plans WHERE code='free'")
        policy=json.loads(original)
        policy['job_concurrent']=1
        await db.execute("UPDATE plans SET quotas=$1 WHERE code='free'",json.dumps(policy))
        await start_windows()
        pool=await create_pool(RedisSettings(host='127.0.0.1',port=worker.broker.port))
        for label,path in cases.items():
            marker=wsl_path(root/(label+'-executions'))
            finish=wsl_path(root/(label+'-finish'))
            script=f"""
from pathlib import Path
import time
with Path({marker!r}).open('a') as f: f.write('once\\n')
deadline=time.monotonic()+140
while not Path({finish!r}).exists():
    if time.monotonic()>deadline: raise TimeoutError()
    time.sleep(.05)
"""
            await call('PUT',path+'/file',{'path':'Ref/slots.py','content':script})
        (root/'armed').touch()
        async def submit(label):
            key=uuid.uuid4().hex
            async def admitted():
                job=await call('POST',cases[label]+'/jobs',{'runtime':'wsl' if label=='windows' else 'windows',
                    'command':'python3 Ref/slots.py','input_paths':['Ref/slots.py'],'timeout_seconds':170},
                    extra_headers={'Idempotency-Key':key},expect=(200,202,409))
                if 'id' in job:return job['id']
                assert job['detail']['code'] in {'storage_quota_busy','validation_busy'},job
            return label,await until(admitted,'Concurrent submission did not settle')
        jobs=dict(await asyncio.gather(*(submit(k) for k in cases)))
        for label in cases:
            await until(lambda:files(f'{label}-request-{jobs[label]}'),'Missing actual claim on '+label)
        assert await slots()==0
        (root/'admit').touch()
        first=await until(lambda:files('*-executions'),'Neither native command began')
        assert len(first)==1
        winner=first[0].name.split('-')[0]
        waiting=next(k for k in cases if k!=winner)
        await until(lambda:files(f'{waiting}-{jobs[waiting]}-deferred'),'Other host did not defer its claim')
        assert (await state(jobs[waiting]))['attempt']['phase']=='queued' and await slots()==1
        assert not (root/(waiting+'-executions')).exists()
        result.update(jobs=jobs,winner=winner,waiting=waiting,occupied_during_contention=1)
        # The owner is stopped while its actual command is still blocked. The
        # other host must subsequently claim the still-original queued attempt.
        if winner=='linux': await worker.stop()
        else: await asyncio.to_thread(windows.stop)
        await until(lambda:files(waiting+'-executions'),'Other host did not acquire the released slot')
        lost=await state(jobs[winner])
        assert lost['status']=='interrupted' and lost['attempt']['process_stop']['verified']
        assert await slots()==1
        (root/(waiting+'-finish')).touch()
        async def completed():
            job=await state(jobs[waiting])
            return job if job['status']=='succeeded' else None
        finished=await until(completed,'Successor did not finish')
        assert finished['attempt']['process_stop']['verified'] and await slots()==0
        if winner=='linux':await asyncio.to_thread(worker.start)
        else:await start_windows()
        for label,identifier in jobs.items():
            marker=root/f'{label}-{identifier}-already_owned_or_terminal'
            marker.unlink(missing_ok=True)
            await pool.enqueue_job('run_delivery',identifier,_job_id='slots-'+uuid.uuid4().hex,
                                   _queue_name=queue_name(targets[label]))
            await until(lambda:files(marker.name),'Actual duplicate delivery did not settle')
            assert (root/(label+'-executions')).read_text().splitlines()==['once']
            counts=await db.fetchrow("""
                SELECT count(DISTINCT a.id) AS attempts,
                  count(e.id) FILTER (WHERE e.type='attempt.state_changed' AND
                    e.payload_json::jsonb->>'status'='claimed') AS claims
                FROM job_attempts a LEFT JOIN durable_events e ON e.attempt_id=a.id WHERE a.job_id=$1
                """,identifier)
            assert counts['attempts']==counts['claims']==1
        assert await slots()==0
        result.update(interrupted_attempt=lost['attempt']['id'],successor_attempt=finished['attempt']['id'],
                      both_process_trees_stopped=True,post_restart_duplicate_claims=0,final_slots=0)
    finally:
        (root/'admit').touch(exist_ok=True)
        for label in cases:(root/(label+'-finish')).touch(exist_ok=True)
        (root/'armed').unlink(missing_ok=True)
        try:
            await asyncio.to_thread(windows.stop)
        finally:
            if pool:await pool.close()
            await db.close()
    result['windows_workers']=windows.receipts
    (root/'host-slots.json').write_text(json.dumps(result,indent=2),encoding='utf8')


async def verify(configuration, selected, admin, observed):
    mapping={'windows':{'host_id':observed['host_id'],'platform':'linux'},
             'wsl':{'host_id':process_identity_native.host_id(),'platform':'windows'}}
    return await remote.verify(configuration,selected,admin,observed,worker_factory=LinuxWorker,
                               after_publish=race,environment_overrides={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps(mapping)})


if __name__=='__main__':
    asyncio.run(remote.main(entry='platform/server/live_llm_cross_host_job_slots_acceptance.py',verifier=verify))
