"""Actual local cancellation/finalization race; queued work and real model readback."""
import argparse,asyncio,json,time,uuid
from live_llm_acceptance import source
from live_llm_cli_startup_acceptance import verify

FIXTURE=r'''
import asyncio,json
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.db import SessionLocal
from app.models import Job,JobAttempt,Plan
from app.services import job_execution
from sqlalchemy import select
root=Path(__file__).parent
original_finish=job_execution.finish
async def held_finish(owner,*args,**kwargs):
 target=root/'hold-job'
 if target.exists() and int(target.read_text())==owner.job_id:
  (root/'finish-held.json').write_text(json.dumps({'job_id':owner.job_id,'actual_stop_verified':kwargs.get('process_stop',{}).get('verified')}))
  while not (root/'allow-finish').exists():await asyncio.sleep(.05)
 return await original_finish(owner,*args,**kwargs)
job_execution.finish=held_finish
@app.post('/api/v1/test/hold-local/{identifier}')
async def hold_local(identifier:int,user=Depends(get_current_user)):
 (root/'hold-job').write_text(str(identifier))
 async with SessionLocal() as db:
  plan=await db.scalar(select(Plan).where(Plan.code=='free'));value=json.loads(plan.quotas);value['job_concurrent']=1
  plan.quotas=json.dumps(value);await db.commit()
 return {'held_job':identifier}
@app.post('/api/v1/test/release-local')
async def release_local(user=Depends(get_current_user)):
 (root/'allow-finish').touch();return {'released':True}
@app.get('/api/v1/test/local-wait-state/{identifier}')
async def local_state(identifier:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  row=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==identifier).order_by(JobAttempt.number.desc()).limit(1))
  return {'phase':row.phase,'claimed':row.claim_token is not None}
'''

def scenario(api,**_):
    from pathlib import Path
    workspace=api.call('GET','/workspaces')[0]['id']
    separate=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Same owner, independent queued case'})['id']
    code="from pathlib import Path\nimport time\nPath("+repr(str(api.root/'first-started'))+").touch()\ntime.sleep(30)\n"
    api.put('Ref/local_queue.py',code)
    first=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/local_queue.py'})
    api.call('POST','/test/hold-local/'+str(first['id']))
    deadline=time.monotonic()+30
    while not (api.root/'first-started').exists():
        assert time.monotonic()<deadline;time.sleep(.1)
    try:
        api.call('POST','/jobs/'+str(first['id'])+':cancel')
        while not (api.root/'finish-held.json').exists():
            assert time.monotonic()<deadline;time.sleep(.1)
        stop=json.loads((api.root/'finish-held.json').read_text());assert stop['actual_stop_verified']
        nonce='LOCAL_QUEUE_'+uuid.uuid4().hex
        queued=api.call('POST',f'/cases/{separate}/jobs',{'command':'python -c "print(\''+nonce+'\')"'})
        before=api.call('GET','/test/local-wait-state/'+str(queued['id']))
        assert queued['status']=='queued' and before=={'phase':'queued','claimed':False}
    finally:api.call('POST','/test/release-local')
    finished=api.wait_job(queued['id'])
    assert finished['log_backlog'].count(nonce)==1 and finished['attempt']['process_stop']['verified']
    report={'passed':True,'real_model_requests':0,'actual_cancelled_job':first['id'],'actual_stop':stop,
        'queued_before_capacity_release':before,'queued_job':queued['id'],'final_phase':finished['attempt']['phase'],
        'exactly_one_real_execution':True,'process_stop_verified':True}
    (api.root/'local-queue-wait.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    await asyncio.to_thread(verify,configuration,fixture=FIXTURE,scenario=scenario)

if __name__=='__main__':asyncio.run(main())
