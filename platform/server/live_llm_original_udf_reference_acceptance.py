"""Actual original UDF publication, restored S3 bytes and Win32 cache lock.

Original SQLite/S3 are read-only. Historical native evidence is preserved;
new reference Jobs and final DeepSeek tool rounds run through actual APIs.
"""
import ast,asyncio,hashlib,json,uuid
from pathlib import Path
from urllib.parse import quote
from live_llm_job_repeat_acceptance import main,write
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_mpi_workflow_acceptance import FIXTURE as API_FIXTURE,vectors
from s3testsupport import ObjectStore
from testsupport import SERVER

SOURCE=SERVER.parents[1]/'tmp/s3-attempt-store-2ejgo90f'
DATABASE=SERVER.parents[1]/'tmp/s3-typed-publication-3pbk6e4r/test.sqlite'
PUBLICATION='ef601298-a1e9-41bc-9621-5cc285889c1d'

EXTRA=r'''
import ctypes,hashlib,json,sqlite3,threading
from pathlib import Path
from ctypes import wintypes
from datetime import datetime,timezone
from sqlalchemy import DateTime,Integer,text
from app.models import User,Workspace,Case,Job,JobAttempt,ArtifactPublication,ArtifactPublicationHead,CaseEventCounter,DurableEvent
from app.services import private_cache
cache_fault_enabled=False
cache_fault_locks=[]
cache_fault_witnesses=[]
cache_fault_mutex=threading.Lock()
cache_cleanup_original=private_cache.Cache.cleanup

def actual_cache_lock(path):
 kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
 kernel.CreateFileW.restype=wintypes.HANDLE
 kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
 handle=kernel.CreateFileW(str(path),0x80000000,0x1|0x2,None,3,0x02000000,None)
 if handle in {None,ctypes.c_void_p(-1).value}:raise ctypes.WinError(ctypes.get_last_error())
 return lambda:kernel.CloseHandle(handle)

def locked_cache_cleanup(cache):
 if not cache_fault_enabled or cache.record['kind']!='.case-read-views' or cache._closed:
  return cache_cleanup_original(cache)
 with cache_fault_mutex:
  if len(cache_fault_locks)>=16:raise RuntimeError('Owned cache lock test exceeded its bound')
 target=cache.root/'D-post' if (cache.root/'D-post').is_dir() else cache.root
 close=actual_cache_lock(target)
 try:
  result=cache_cleanup_original(cache)
  witness={'name':cache.container.name,'kind':cache.record['kind'],'actual_result':result,
   'owner_preserved':(cache.container/'owner.json').exists(),'journal_preserved':(cache.container/'files.jsonl').exists(),
   'writers_closed':cache._closed,'actual_retained_bytes':sum(p.stat().st_size for p in cache.container.rglob('*') if p.is_file())}
  with cache_fault_mutex:
   cache_fault_locks.append((cache,close));cache_fault_witnesses.append(witness)
  return result
 except BaseException:close();raise
private_cache.Cache.cleanup=locked_cache_cleanup

@app.post('/api/v1/testing/restore-original-udf')
async def restore_original_udf(user=Depends(get_current_user),db=Depends(get_db)):
 assert db.get_bind().dialect.name=='postgresql'
 assert __import__('re').fullmatch('tmp_vf_repeat_[0-9a-f]{32}',db.get_bind().url.database)
 assert await db.get(Case,10) is None
 path=Path(ORIGINAL_SQLITE);before=hashlib.sha256(path.read_bytes()).hexdigest()
 assert before=='fb005cbcac13ecf631bb2cc4556ba3959e6c6a3a0d781e8f2d4569603cb8c0d7'
 source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);source.row_factory=sqlite3.Row
 restored={}
 try:
  requests=[(User,'id=10'),(Workspace,'id=10'),(Case,'id=10'),(Job,'case_id=10'),
   (JobAttempt,'job_id IN (16,17)'),(ArtifactPublication,'case_id=10'),(ArtifactPublicationHead,'case_id=10'),
   (CaseEventCounter,'case_id=10'),(DurableEvent,'case_id=10')]
  for model,condition in requests:
   rows=source.execute('SELECT * FROM '+model.__tablename__+' WHERE '+condition).fetchall();restored[model.__tablename__]=len(rows)
   for row in rows:
    values={}
    for column in model.__table__.columns:
     if column.name not in row.keys():continue
     value=row[column.name]
     if isinstance(column.type,DateTime) and value is not None:
      value=datetime.fromisoformat(value)
      if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
     values[column.name]=value
    db.add(model(**values))
   await db.flush()
  for model in [User,Workspace,Case,Job,DurableEvent]:
   sequence=await db.scalar(text("SELECT pg_get_serial_sequence(:table,'id')"),{'table':model.__tablename__})
   value=await db.scalar(select(__import__('sqlalchemy').func.max(model.id)))
   if sequence and value is not None:await db.execute(text('SELECT setval(CAST(:sequence AS regclass),:value,true)'),{'sequence':sequence,'value':value})
  await db.commit();owner=await db.get(User,10)
  assert hashlib.sha256(path.read_bytes()).hexdigest()==before
  return {'original_owner_email':owner.email,'original_database_unchanged':True,'restored_rows':restored}
 finally:source.close()

@app.post('/api/v1/cases/{case_id}/testing/cache-sharing')
async def control_cache_sharing(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 global cache_fault_enabled
 await get_owned_case(case_id,user,db,write=True)
 if body['action']=='enable':
  assert not cache_fault_locks;cache_fault_witnesses.clear();cache_fault_enabled=True;return {'enabled':True}
 if body['action']=='inspect':
  rows=private_cache.recover();return {'witnesses':cache_fault_witnesses,
   'active_owner_rows':[row for row in rows if row.get('name') in {cache.container.name for cache,_ in cache_fault_locks}]}
 assert body['action']=='release';cache_fault_enabled=False;results=[]
 with cache_fault_mutex:pending=list(cache_fault_locks);cache_fault_locks.clear()
 for cache,close in pending:
  close();result=cache_cleanup_original(cache)
  results.append({'name':cache.container.name,'actual_result':result,'container_exists':cache.container.exists()})
 return {'actual_cleanup':results}

@app.get('/api/v1/cases/{case_id}/testing/original-udf-history')
async def original_udf_history(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 await get_owned_case(case_id,user,db,write=False)
 rows=[]
 for model,condition in [(Job,Job.id.in_([16,17])),(JobAttempt,JobAttempt.job_id.in_([16,17])),
  (ArtifactPublication,ArtifactPublication.case_id==case_id),(ArtifactPublicationHead,ArtifactPublicationHead.case_id==case_id)]:
  for row in await db.scalars(select(model).where(condition)):
   rows.append({'table':model.__tablename__,'fields':{c.name:str(getattr(row,c.name)) for c in model.__table__.columns}})
 rows.sort(key=lambda row:json.dumps(row,sort_keys=True))
 return {'sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()}
'''
FIXTURE=API_FIXTURE+'\nORIGINAL_SQLITE='+repr(str(DATABASE))+'\n'+EXTRA


def scenario(api,*,store=None,**_):
    assert store is not None and store.restore_from==SOURCE.resolve()
    (api.root/'original-udf-reference-entry-source.py').write_bytes(Path(__file__).read_bytes())
    restored=api.call('POST','/testing/restore-original-udf',{})
    auth=api.call('POST','/auth/login',{'email':restored.pop('original_owner_email'),'password':'isolated-test-123'})
    api.token=auth['token'];api.case_id=10;api.prefix='/cases/10'
    checks=[]
    def record(value):
        checks.append(value);(api.root/'original-udf-reference-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
        print('ORIGINAL_UDF_REFERENCE_STAGE '+value['stage'],flush=True)
    def drain(jid):
        job=api.wait_job(jid,timeout=180)
        wait_for(lambda:api.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual reference writer did not drain',90)
        wait_for(lambda:not api.call('GET','/jobs/'+str(jid))['completion_pending'],'Reference completion did not settle',90)
        return api.call('GET','/jobs/'+str(jid))
    original=api.call('GET','/testing/workflow-publication/'+PUBLICATION)
    assert original['manifest']['case_id']==10 and original['manifest']['attempt_id']=='c4008554-8898-4ad4-93c1-2bb9f1fe71c1'
    actual_field=api.call('GET',api.prefix+'/raw?path='+quote('C-run/case-1/0.5/U'),raw=True)
    assert len(vectors(actual_field))==400
    history=api.call('GET',api.prefix+'/testing/original-udf-history')
    record({'stage':'actual_original_SQLite_and_S3_restored','original_case':10,'original_success_job':16,'original_failed_reference_job':17,
        'original_publication':PUBLICATION,'original_manifest_sha256':'ef6a76de9aa8df3a188fb85cbf13682bd86a2d471ce78fcaf5a8f979cf2e0a1a',
        'original_400_cell_result_read':True,'original_SQLite_read_only':True,'restored_rows':restored['restored_rows']})
    try:
        api.call('POST',api.prefix+'/testing/cache-sharing',{'action':'enable'})
        created=write(api,'POST',api.prefix+'/jobs',{'command':'echo ORIGINAL_UDF_CACHE_REFERENCE',
            'input_publications':[{'publication_id':PUBLICATION,'require_current':True}]})
        job=drain(created['id']);assert job['id'] not in {16,17}
        assert 'ORIGINAL_UDF_CACHE_REFERENCE' in api.call('GET','/jobs/'+str(job['id'])+'/log?offset=0')['chunk']
        proof=api.call('POST',api.prefix+'/testing/cache-sharing',{'action':'inspect'})
        assert proof['witnesses'] and proof['active_owner_rows']
        assert all(v['actual_result']=={'status':'retained','reason':'sharing_violation','winerror':32}
            and v['owner_preserved'] and v['journal_preserved'] and v['writers_closed'] and v['actual_retained_bytes']>0 for v in proof['witnesses'])
        assert all(v['status']=='active' for v in proof['active_owner_rows'])
        assert api.call('GET',api.prefix+'/testing/original-udf-history')==history
        assert api.call('GET','/testing/workflow-publication/'+PUBLICATION)==original
        record({'stage':'real_current_reference_succeeded_with_actual_Win32_locks','actual_new_reference_job':job['id'],
            'actual_reference_stop_verified':job['attempt']['process_stop']['verified'],'actual_cache_proof':proof,
            'original_success_failure_and_publication_unchanged':True,'new_native_build_claimed':False})
    finally:
        released=api.call('POST',api.prefix+'/testing/cache-sharing',{'action':'release'})
        (api.root/'actual-cache-release.json').write_text(json.dumps(released,indent=2),encoding='utf8')
    assert released['actual_cleanup'] and all(v['actual_result']['status']=='removed' and not v['container_exists'] for v in released['actual_cleanup'])
    created=write(api,'POST',api.prefix+'/jobs',{'command':'echo ORIGINAL_UDF_REFERENCE_AFTER_RELEASE',
        'input_publications':[{'publication_id':PUBLICATION,'require_current':True}]})
    drain(created['id']);assert api.call('GET',api.prefix+'/testing/original-udf-history')==history
    record({'stage':'actual_lock_release_cleanup_and_second_reference','actual_cleanup':released,'actual_second_reference_job':created['id'],
        'original_field_unchanged':api.call('GET',api.prefix+'/raw?path='+quote('C-run/case-1/0.5/U'),raw=True)==actual_field})
    report={'passed':True,'mock_llm_calls':0,'checks':checks,'scope':'Original real historical UDF bytes/SQL publication preserved in isolated copies; new actual current-reference Jobs under real Win32 locks. Original precise error/holder were not logged; no new OpenFOAM execution is claimed here.'}
    (api.root/'original-udf-reference.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent actual original UDF reference readback');return report


def restored_store():
    return ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',volume_max=16,restore_from=SOURCE)


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16,store_factory=restored_store))
