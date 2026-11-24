"""Read API recovery credit, error settlement, shared access and writer drain."""
import argparse,asyncio,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import INSPECT
from owner_storage_budget_smoke import OBSERVE
from file_owner_storage_smoke import FIXTURE as CONFIGURE
from s3_file_transactions_smoke import FIXTURE as WRITER
from job_release_smoke import wait_file

POLICY='''
from contextlib import asynccontextmanager
from fastapi import HTTPException
from app.services import case_read_capacity
original_hold_case=case_read_capacity.hold_case
read_task=None
@asynccontextmanager
async def controlled_hold(*args,**kwargs):
 global read_task
 if (root/'force-busy').exists():
  read_task=asyncio.current_task()
  with (root/'busy-attempts').open('a') as stream:stream.write('attempt\\n')
  raise HTTPException(409,{'code':'storage_quota_busy'})
 async with original_hold_case(*args,**kwargs) as owner:yield owner
case_read_capacity.hold_case=controlled_hold
@app.post('/api/v1/fixture/read-policy')
async def read_policy(body:dict):
 from app.config import get_settings
 get_settings().storage_measurement_timeout_seconds=body['seconds']
 if body.get('cancel'):
  assert read_task is not None;read_task.cancel()
 return {'set':True}
'''


def verify(store,modes,url=None):
 report=Path(tempfile.mkdtemp(prefix='read-owner-storage-runs-',dir=SERVER.parents[1]/'tmp'))
 database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
 store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
 api=IsolatedServer('read-owner-storage',app='read_owner:app',database_url=url)
 settings={**store.settings(),'VIBEFLOW_DATABASE_URL':database}
 (api.root/'read_owner.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+WRITER+INSPECT+OBSERVE+CONFIGURE+POLICY,encoding='utf8')
 checks=[]
 with api:
  case=api.create_case('Read owner storage');owner=api.call('GET','/auth/me')['id'];api.put('Ref/text.txt','original');api.put('Ref/binary.bin','binary')
  api.call('POST','/case-provisions:recover');endpoint='/api/v1'+api.prefix
  def state():return api.call('GET',f'/fixture/owner/{case["id"]}')
  def settled():
   data=state();assert all(r['state']=='settled' for r in data['rows']),data
   assert data['rows'][-1]['receipt']['closed'];return data
  with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
   if 'policy' in modes:
    before=state();(api.root/'force-busy').touch()
    api.call('POST','/fixture/read-policy',{'seconds':.2})
    started=time.monotonic();response=client.get(endpoint+'/file?path=Ref/text.txt');elapsed=time.monotonic()-started
    assert response.status_code==409 and .18<=elapsed<3,(response.status_code,elapsed)
    attempts=(api.root/'busy-attempts').read_text().splitlines();assert 2<=len(attempts)<=7,attempts
    assert state()['rows']==before['rows'] and state()['usage']['storage_bytes_used']==before['usage']['storage_bytes_used']
    checks.append('busy_retry_deadline_no_recovery_replay');print('PASS '+checks[-1],flush=True)
    (api.root/'busy-attempts').unlink();api.call('POST','/fixture/read-policy',{'seconds':30})
    with ThreadPoolExecutor(max_workers=1) as pool:
     future=pool.submit(client.get,endpoint+'/file?path=Ref/text.txt')
     wait_file(api.root/'busy-attempts',seconds=10)
     api.call('POST','/fixture/read-policy',{'seconds':30,'cancel':True})
     assert future.result(timeout=5).status_code==500
    assert state()['rows']==before['rows']
    (api.root/'force-busy').unlink()
    assert client.get(endpoint+'/file?path=Ref/text.txt').status_code==200;settled()
    checks.append('cancel_busy_wait_without_claim');print('PASS '+checks[-1],flush=True)
   if 'routes' in modes:
    for name,method,suffix in [('tree','GET','/tree'),('text','GET','/file?path=Ref/text.txt'),('binary','GET','/file?path=Ref/binary.bin'),
       ('version','GET','/file:version?path=Ref/text.txt'),('raw','GET','/raw?path=Ref/binary.bin'),('head','HEAD','/raw?path=Ref/binary.bin'),('dictionary','GET','/caseDict')]:
     (api.root/f'quota-{owner}').write_text('0');before=state()['usage']['storage_bytes_used']
     denied=client.request(method,endpoint+suffix);assert denied.status_code==429,(name,denied.status_code,denied.text)
     assert settled()['usage']['storage_bytes_used']==before
     (api.root/f'quota-{owner}').write_text('4')
     response=client.request(method,endpoint+suffix);assert response.status_code==200,(name,response.text)
     assert settled()['rows'][-1]['receipt']['used']>0
     if name=='text':assert response.json()['content']=='original'
     if name=='raw':assert response.content==b'binary'
     if name=='head':assert response.content==b'' and response.headers['content-length']=='6'
     checks.append(name+'_recovery_quota_and_settlement');print('PASS '+checks[-1],flush=True)
   if 'errors' in modes:
    for name,suffix,headers,status in [('missing','/file?path=Ref/missing.txt',{},404),('raw_missing','/raw?path=Ref/missing.bin',{},404),
      ('version_conflict','/raw?path=Ref/text.txt',{'X-Vibeflow-File-Version':'0'*64},409),
      ('range','/raw?path=Ref/text.txt',{'Range':'bytes=9999-'},416),('path','/raw?path=../outside',{},422)]:
     response=client.get(endpoint+suffix,headers=headers);assert response.status_code==status,(name,response.text)
     settled();checks.append(name+'_settles_credit');print('PASS '+checks[-1],flush=True)
   if 'busy' in modes:
    with ThreadPoolExecutor(max_workers=2) as pool:
     (api.root/'mode').write_text('after_one')
     writing=pool.submit(api.put,'Ref/text.txt','committed')
     wait_file(api.root/'ready',seconds=30)
     reading=pool.submit(client.get,endpoint+'/file?path=Ref/text.txt')
     try:
      time.sleep(.3);assert not reading.done(),'Read should wait while a known live writer drains'
     finally:(api.root/'release').touch()
     writing.result(timeout=30);response=reading.result(timeout=30)
     assert response.status_code==200 and response.json()['content']=='committed',response.text
    (api.root/'mode').write_text('');settled();checks.append('read_waits_for_writer_credit');print('PASS '+checks[-1],flush=True)
   if 'shared' in modes:
    registered=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Shared reader'})
    token=api.token;api.token=registered['token'];member=api.call('GET','/auth/me')['id'];api.token=token
    api.call('POST',f'/fixture/share/{case["id"]}/{member}')
    (api.root/f'quota-{member}').write_text('0')
    before=len(state()['rows']);api.token=registered['token']
    response=api.call('GET',api.prefix+'/file?path=Ref/text.txt');assert response['kind']=='text'
    assert state()['rows']==[]
    api.token=token;assert len(settled()['rows'])==before+1
    checks.append('shared_reader_charges_workspace_owner');print('PASS '+checks[-1],flush=True)
 result={'passed':True,'count':len(checks),'checks':checks,'api':str(api.root)}
 (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8');return result


async def main():
 parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
 modes=args.modes or ['routes','errors','busy','shared','policy']
 from s3testsupport import ObjectStore
 from s3_typed_publication_smoke import RUNTIME
 report=Path(tempfile.mkdtemp(prefix='read-owner-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
 with ObjectStore(executable=RUNTIME) as store:results={'sqlite':await asyncio.to_thread(verify,store,modes)}
 if args.postgres:
  import asyncpg
  from sqlalchemy.engine import make_url
  from app.config import Settings
  from workflow_migration_smoke import alembic
  url=make_url(Settings(_env_file=SERVER/'.env').database_url)
  assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
  name='tmp_vf_readowner_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_readowner_[0-9a-f]{32}',name)
  admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
  created=False
  try:
   await admin.execute('CREATE DATABASE "'+name+'"');created=True
   target=url.set(database=name).render_as_string(hide_password=False);await asyncio.to_thread(alembic,target,report,'upgrade','head')
   with ObjectStore(executable=RUNTIME) as store:results['postgresql']=await asyncio.to_thread(verify,store,modes,target)
  finally:
   if created:await admin.execute('DROP DATABASE "'+name+'"')
   await admin.close()
  results['postgresql']['temporary_database_removed']=True
 (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
 print('READ_OWNER_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
