"""Committed repeat replays during a real S3 owner write hold; real model readback."""
import asyncio,json,time,uuid
from concurrent.futures import ThreadPoolExecutor
from live_llm_job_repeat_acceptance import main,repeat
from testsupport import IsolatedServer

HOLD=r'''
import asyncio,time
from pathlib import Path
from fastapi import Depends
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import owner_storage_budget
@app.post('/api/v1/test/hold-repeat-storage/{case_id}')
async def hold_repeat_storage(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 assert storage._use_s3()
 case=await get_owned_case(case_id,user,db,write=True);directory=Path(__file__).parent
 async with owner_storage_budget.hold_case(db,case):
  assert owner_storage_budget.current() is not None
  (directory/'write-capacity-held').touch();deadline=time.monotonic()+20
  while not (directory/'release-write-capacity').exists():
   if time.monotonic()>deadline:raise TimeoutError('Actual write hold was not released')
   await asyncio.sleep(.02)
  await db.rollback()
 return {'released':True}
'''


def scenario(api,*,store=None,**_):
    assert store is not None,'This acceptance requires --s3'
    api.put('Ref/known-failure.py','raise SystemExit(17)\n')
    first=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/known-failure.py'})
    api.wait_job(first['id'],expected='failed')
    options=api.call('GET','/jobs/'+str(first['id'])+'/operations')
    body={'action':'retry','source_attempt_id':options['source_attempt_id'],'source_version':options['source_version']}
    key=uuid.uuid4().hex;new=repeat(api,first['id'],body,key);failed=api.wait_job(new['id'],expected='failed')
    assert failed['attempt']['process_stop']['verified']
    peer=IsolatedServer('readonly-repeat-peer',app='peer_fixture:app')
    shared={key:value for key,value in api.env.items() if key.startswith('VIBEFLOW_')};shared.update(store.settings())
    (peer.root/'peer_fixture.py').write_text('import os\nos.environ.update('+repr(shared)+')\nfrom app.main import app\n'+HOLD,encoding='utf8')
    with peer:
        peer.token=api.token
        with ThreadPoolExecutor(max_workers=1) as pool:
            hold=pool.submit(peer.call,'POST','/test/hold-repeat-storage/'+str(api.case_id))
            try:
                deadline=time.monotonic()+10
                while not (peer.root/'write-capacity-held').exists():
                    if hold.done():hold.result();raise AssertionError('Write hold returned before release')
                    if time.monotonic()>deadline:raise TimeoutError('Real S3 write hold did not become ready')
                    time.sleep(.03)
                started=time.monotonic()
                replay=api.call('POST','/jobs/'+str(first['id'])+':repeat',body,expect=202,extra_headers={'Idempotency-Key':key})
                duration=time.monotonic()-started
                assert replay['id']==new['id'] and not replay['created'] and not hold.done()
                assert not (peer.root/'release-write-capacity').exists()
            finally:(peer.root/'release-write-capacity').touch()
            assert hold.result(timeout=10)=={'released':True}
    jobs=api.call('GET',api.prefix+'/jobs');assert len(jobs)==2 and all(j['status']=='failed' for j in jobs)
    report={'passed':True,'mock_llm_calls':0,'real_model_requests':0,'actual_s3_owner_write_hold':True,
        'two_independent_apis':True,'committed_replay_returned_while_hold_active':True,'replay_seconds':round(duration,4),
        'source_job':first['id'],'retry_job':new['id'],'actual_native_attempts':2,'expected_native_exit_code':17,
        'duplicate_execution':False,'peer_exited':peer.proc.poll() is not None,'peer_root':str(peer.root)}
    (api.root/'readonly-repeat-replay.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario))
