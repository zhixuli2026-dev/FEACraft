"""Local journal allocation peaks, recovery, bounded copies and drained locks."""
import argparse,asyncio,json,re,tempfile,time,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from testsupport import IsolatedServer,SERVER
from file_transactions_smoke import FIXTURE,peer
from local_provision_storage_smoke import OBSERVE
from node_storage_budget_smoke import stop
from job_release_smoke import wait_file

EXTRA='''
from app.services import node_storage_budget as node,local_file_budget
from app.config import get_settings
active_task=None
original_write=journal.write
async def tracked(*args,**kwargs):
 global active_task
 active_task=asyncio.current_task()
 try:return await original_write(*args,**kwargs)
 finally:
  if (root/'ready').exists():(root/'ended').touch()
journal.write=tracked
@app.post('/api/v1/fixture/cancel')
async def cancel():
 assert active_task is not None;active_task.cancel()
 return {'cancelled':True}
original_admit=node._admit
def admitting(size):
 p=storage._fs_root()/'.test-node-limit'
 if p.exists():get_settings().node_storage_limit_mb=int(p.read_text())
 get_settings().node_storage_min_free_mb=0
 return original_admit(size)
node._admit=admitting
original_replace=journal.os.replace
def replacing(source,target):
 if Path(target).name=='fault.bin' and mode() in {'peak','cancel'}:
  (root/'temporary.json').write_text(json.dumps({'path':str(source),'size':Path(source).stat().st_size}))
  hold()
 return original_replace(source,target)
journal.os.replace=replacing
original_capture=journal._capture
def capturing(path,destination=None,**kwargs):
 if mode()=='source_growth' and destination is not None and Path(destination).parent.name=='after':
  with Path(path).open('ab') as stream:stream.write(b'x'*65536)
 return original_capture(path,destination,**kwargs)
journal._capture=capturing
@app.post('/api/v1/fixture/source/{case_id}')
async def source(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 async with coordinate(db,case):
  path='Ref/source.bin';captured=await journal._io(journal._capture,storage._fs_path(case.s3_prefix,path))
  await journal.write(db,case,{'Ref/fault.bin':journal.FileSource(path,captured)})
 await db.commit();await journal.finish_committed(db,case)
 return {'copied':True}
'''


def size(root):return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
def claims(base):return list((base/'.node-storage-budget').glob('*.json'))


def verify(modes,url=None):
 report=Path(tempfile.mkdtemp(prefix='local-file-storage-runs-',dir=SERVER.parents[1]/'tmp'));checks=[];runs=[]
 for mode in modes:
  api=IsolatedServer('local-file-storage-'+mode,app='local_file:app',database_url=url)
  (api.root/'local_file.py').write_text(FIXTURE+OBSERVE+EXTRA,encoding='utf8')
  with api:
   case=api.create_case('Local file storage '+mode);api.call('POST','/case-provisions:recover')
   owner=api.call('GET','/auth/me')['id'];base=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT']);owned=base/f'workspaces/{owner}'
   case_root=next(owned.glob(f'cases/{case["id"]}'));journal=case_root/'.vibeflow/file-transactions'
   old=b'o'*(256*1024 if mode in {'before','recovery'} else 1024)
   api.put('Ref/fault.bin',old.decode());target=case_root/'Ref/fault.bin'
   baseline=size(owned);result={}
   def limit(available):(base/'.test-quota-limit').write_text(str(size(owned)+available))
   if mode in {'before','after','metadata','node'}:
    new=b'n'*(128*1024 if mode in {'after','node'} else 0 if mode=='metadata' else 4096)
    if mode=='node':
     (base/'.test-node-limit').write_text('1')
     (base/'.node-filler').write_bytes(b'f'*(1024*1024-size(base)-128*1024))
    else:limit({'before':128*1024,'after':240*1024,'metadata':0}[mode])
    before={str(p):p.read_bytes() for p in journal.rglob('*') if p.is_file()}
    reply=api.call('PUT',api.prefix+'/file',{'path':'Ref/fault.bin','content':new.decode()},expect=409 if mode=='node' else 429)
    if mode=='node':assert reply['detail']['code']=='node_storage_full',reply
    assert target.read_bytes()==old and size(owned)==baseline
    assert {str(p):p.read_bytes() for p in journal.rglob('*') if p.is_file()}==before
    assert not claims(base)
    (base/'.test-quota-limit').write_text(str(8*1024*1024));(base/'.test-node-limit').write_text('16')
    api.put('Ref/fault.bin',new.decode());assert target.read_bytes()==new
    result={'refused_before_allocating':True}
   elif mode in {'source','source_growth'}:
    source=case_root/'Ref/source.bin';source.write_bytes(b's'*(128*1024))
    limit(240*1024);before=target.read_bytes()
    api.call('POST',f'/fixture/source/{case["id"]}',expect=429)
    assert target.read_bytes()==before
    (base/'.test-quota-limit').write_text(str(8*1024*1024))
    if mode=='source_growth':(api.root/'mode').write_text(mode)
    api.call('POST',f'/fixture/source/{case["id"]}',expect=500 if mode=='source_growth' else 200,raw=mode=='source_growth')
    if mode=='source_growth':
     assert target.read_bytes()==before and not (journal/'pending').exists()
     assert all(p.stat().st_size<=128*1024 for p in journal.glob('preparing-*/after/*'))
    else:assert target.read_bytes()==source.read_bytes()
    assert not claims(base)
    result={'bounded_source_copy':True}
   else:
    (base/'.test-quota-limit').write_text(str(8*1024*1024))
    mode_value='after_apply' if mode=='recovery' else mode
    (api.root/'mode').write_text(mode_value)
    new=b'n'*(16*1024 if mode=='recovery' else 128*1024)
    reader=peer(api);script=reader.root/'journal_peer.py';script.write_text(script.read_text(encoding='utf8')+FIXTURE+OBSERVE+EXTRA,encoding='utf8')
    with reader,ThreadPoolExecutor(max_workers=2) as pool:
     future=pool.submit(api.call,'PUT',api.prefix+'/file',{'path':'Ref/fault.bin','content':new.decode()},expect=500 if mode=='cancel' else 200,raw=mode=='cancel')
     wait_file(api.root/'ready',seconds=30)
     held=claims(base);assert len(held)==1,held
     claim=json.loads(held[0].read_text());assert size(owned)-baseline<=claim['bytes']
     if mode in {'peak','cancel'}:
      temporary=json.loads((api.root/'temporary.json').read_text());assert Path(temporary['path']).read_bytes()==new
      if mode=='cancel':
       api.call('POST','/fixture/cancel');time.sleep(.2)
       assert not (api.root/'ended').exists() and claims(base),'Cancelled writer released claim before drain'
      if url:
       blocked=reader.call('POST','/fixture/compete',expect=409);assert blocked['detail']['code']=='storage_quota_busy';competing=None
      else:
       competing=pool.submit(reader.call,'POST','/fixture/compete');time.sleep(.2);assert not competing.done()
      (api.root/'release').touch();future.result(timeout=30)
      if competing is not None:assert competing.result(timeout=20)['admitted']
      assert reader.call('GET',api.prefix+'/raw?path=Ref/fault.bin',raw=True)==(old if mode=='cancel' else new)
     else:
      stop(api.proc)
      try:future.result(timeout=15)
      except OSError:pass
      else:raise AssertionError('Terminated writer returned success')
      assert target.read_bytes()==new and claims(base)
      limit(len(old)-1)
      reader.call('GET',api.prefix+'/raw?path=Ref/fault.bin',expect=429)
      assert target.read_bytes()==new and (journal/'pending').exists()
      (base/'.test-quota-limit').write_text(str(8*1024*1024))
      (base/'.test-node-limit').write_text('1')
      (base/'.node-filler').write_bytes(b'f'*(1024*1024-size(base)-32*1024))
      refused=reader.call('GET',api.prefix+'/raw?path=Ref/fault.bin',expect=409)
      assert refused['detail']['code']=='node_storage_full' and target.read_bytes()==new
      (base/'.test-node-limit').write_text('16')
      assert reader.call('GET',api.prefix+'/raw?path=Ref/fault.bin',raw=True)==old
     assert not claims(base) and not (journal/'pending').exists()
     result={'held_claim':claim,'actual_peak_delta':size(owned)-baseline,'reader':str(reader.root)}
   (api.root/'storage-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
   checks.append(mode);runs.append({'mode':mode,'api':str(api.root)})
   print('PASS local file storage '+mode,flush=True)
 result={'passed':True,'count':len(checks),'checks':checks,'runs':runs}
 (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8');return result


async def main():
 parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
 modes=args.modes or ['before','after','metadata','node','source','source_growth','peak','cancel','recovery']
 report=Path(tempfile.mkdtemp(prefix='local-file-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
 results={'sqlite':await asyncio.to_thread(verify,modes)}
 if args.postgres:
  import asyncpg
  from sqlalchemy.engine import make_url
  from app.config import Settings
  from workflow_migration_smoke import alembic
  url=make_url(Settings(_env_file=SERVER/'.env').database_url)
  assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
  name='tmp_vf_localfile_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_localfile_[0-9a-f]{32}',name)
  admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
  created=False
  try:
   await admin.execute('CREATE DATABASE "'+name+'"');created=True
   target=url.set(database=name).render_as_string(hide_password=False)
   await asyncio.to_thread(alembic,target,report,'upgrade','head')
   results['postgresql']=await asyncio.to_thread(verify,modes,target)
  finally:
   if created:await admin.execute('DROP DATABASE "'+name+'"')
   await admin.close()
  results['postgresql']['temporary_database_removed']=True
 (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
 print('LOCAL_FILE_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
