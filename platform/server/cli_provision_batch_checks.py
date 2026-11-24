"""Real local writes, claim retention and cancellation for creation batches."""
FIXTURE=r'''
import asyncio,json,threading,uuid
from pathlib import Path
from fastapi import Depends,HTTPException
from app.deps import get_current_user
from app.db import SessionLocal
from app import storage
from app.config import get_settings
from app.services import case_provision as provision,local_provision_budget as budget,node_storage_budget as node,quota
from app.services.async_ops import finish_before_cancel
from app.services.case_write import CaseWriteConflict
node_admissions=[]
original_capacity=node._capacity
def measured_capacity(*args,**kwargs):
 result=original_capacity(*args,**kwargs);node_admissions.append(result);return result
node._capacity=measured_capacity

@app.post('/api/v1/test/provision-batch')
async def provision_batch(user=Depends(get_current_user)):
 result={'passed':False,'checks':[],'mock_llm_calls':0}
 if storage._use_s3():
  result.update(passed=True,backend='s3',local_batch_used=False)
  return result
 result['backend']='fs'
 # The ordinary case created by the real driver used one node inventory.
 assert len(node_admissions)==1,len(node_admissions)
 result['normal_creation_node_scans']=len(node_admissions)
 base=storage._fs_root();claims=base/node.DIRECTORY
 def active():return list(claims.glob('*.json'))
 assert not active()
 prefix=f'workspaces/{user.id}/.case-provisions'
 for mode in ('underbound','user_quota','foreign_owner'):
  name='batch-'+mode+'-'+uuid.uuid4().hex+'.txt';target=provision.local_path(prefix,name)
  original_limit=quota._storage_limit
  if mode=='user_quota':quota._storage_limit=lambda settings:0
  try:
   async with SessionLocal() as db:
    async with budget.hold(db,user.id):
     async with budget.node_batch(user.id,1 if mode=='underbound' else 65536):
      assert len(active())==1
      try:
       await finish_before_cancel(asyncio.to_thread(provision.put_new,
        prefix if mode!='foreign_owner' else f'workspaces/{user.id+999}/.case-provisions',name,b'actual bounded write'))
      except (ValueError,HTTPException) as error:
       if mode=='user_quota':assert isinstance(error,HTTPException) and error.status_code==429
       else:assert isinstance(error,ValueError)
      else:raise AssertionError('Unadmitted creation write succeeded')
  finally:quota._storage_limit=original_limit
  assert not target.exists() and not active()
  result['checks'].append({'mode':mode,'rejected_before_write':True,'claim_released':True})
 # Force actual node admission to reject a physical 2MiB owned test file.
 filler=base/('batch-filler-'+uuid.uuid4().hex);filler.write_bytes(b'x'*(2*1024*1024))
 settings=get_settings();old_limit=settings.node_storage_limit_mb
 try:
  settings.node_storage_limit_mb=1
  try:
   async with budget.node_batch(user.id,1):raise AssertionError('Full node admitted a batch')
  except CaseWriteConflict as error:assert error.code=='node_storage_full'
 finally:settings.node_storage_limit_mb=old_limit;filler.unlink()
 assert not active();result['checks'].append({'mode':'node_full','refused':True})
 ready=threading.Event();release=threading.Event();observed={}
 name='batch-cancel-'+uuid.uuid4().hex+'.bin';target=provision.local_path(prefix,name)
 original_link=provision.os.link
 def linked(source,destination,*args,**kwargs):
  value=original_link(source,destination,*args,**kwargs)
  if Path(destination)==target:
   observed.update(source=str(source),target=str(destination),intent=str(Path(source).with_suffix('.json')))
   ready.set()
   if not release.wait(15):raise TimeoutError('Actual batch writer was not released')
  return value
 async def writer():
  async with SessionLocal() as db:
   async with budget.hold(db,user.id):
    async with budget.node_batch(user.id,65536):
     await finish_before_cancel(asyncio.to_thread(provision.put_new,prefix,name,b'C'*8192))
 task=None;provision.os.link=linked
 try:
  task=asyncio.create_task(writer())
  for _ in range(500):
   if ready.is_set() or task.done():break
   await asyncio.sleep(.01)
  assert ready.is_set()
  assert Path(observed['source']).samefile(target) and Path(observed['intent']).is_file()
  assert len(active())==1 and json.loads(active()[0].read_text())['bytes']==65536
  task.cancel();await asyncio.sleep(.1)
  assert not task.done() and len(active())==1
  release.set();outcome=(await asyncio.gather(task,return_exceptions=True))[0]
  assert isinstance(outcome,asyncio.CancelledError)
  assert target.read_bytes()==b'C'*8192 and not active()
  assert not Path(observed['source']).exists() and not Path(observed['intent']).exists()
  result['checks'].append({'mode':'cancel_during_hardlink','claim_held_until_writer_drained':True,'target_preserved':True})
 finally:
  release.set()
  if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
  provision.os.link=original_link
 result['passed']=True
 (Path(__file__).parent/'provision-batch-checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
 return result
'''


def verify(api,**_):
    import httpx
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=90,trust_env=False) as client:
        response=client.post('/test/provision-batch');response.raise_for_status();result=response.json()
    assert result['passed']
    print('PROVISION_BATCH_PASS '+str(api.root),flush=True)
    return result


def verify_copy(api,**_):
    import httpx,json
    payload='真实复制容量检查\n'*32768
    api.put('Ref/batch-copy.txt',payload)
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=90,trust_env=False) as client:
        response=client.post('/cases/'+str(api.case_id)+':copy',json={'name':'Actual bounded large copy'})
        response.raise_for_status();copied=response.json()
        response=client.get('/cases/'+str(copied['id'])+'/raw',params={'path':'Ref/batch-copy.txt'})
        response.raise_for_status();assert response.content==payload.encode('utf8')
        response=client.post('/case-provisions:recover');response.raise_for_status();recovered=response.json()
        assert recovered['recovery_required']==0
    result={'passed':True,'actual_copy_bytes':len(payload.encode('utf8')),'source_case':api.case_id,
        'copied_case':copied['id'],'actual_recovery':recovered,'mock_llm_calls':0}
    (api.root/'provision-batch-copy.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('PROVISION_BATCH_COPY_PASS '+str(api.root),flush=True);return result
