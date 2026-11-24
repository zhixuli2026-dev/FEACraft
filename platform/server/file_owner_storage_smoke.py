"""Real file API quota: physical versions, shared owners, and preview handoff."""
import argparse,asyncio,json,re,tempfile,uuid
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import INSPECT
from owner_storage_budget_smoke import OBSERVE
from case_maintenance_storage_smoke import read

FIXTURE='''
async def quotas(db,user):
 p=root/('quota-'+str(user.id))
 return {**quota.DEFAULT_QUOTAS,'storage_mb':int(p.read_text()) if p.exists() else 4}
quota.quotas_for=quotas
from app.routers import geometry
async def preview(case_id,kind,user,db):
 assert budget.current() is None,'File credit leaked into a future Job reservation'
 await quota.check_storage_quota(db,user,1)
 (root/'preview-ok').touch()
 return {'job_id':731}
geometry._launch=preview
@app.post('/api/v1/fixture/share/{case_id}/{member_id}')
async def share(case_id:int,member_id:int,user:User=Depends(get_current_user)):
 from app.models import Workspace,Organization,Membership
 from app.services.sharing import set_share
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  workspace=await db.get(Workspace,case.workspace_id)
  assert workspace.user_id==user.id
  org=Organization(name='Quota fixture');db.add(org);await db.flush()
  db.add_all([Membership(org_id=org.id,user_id=user.id,role='admin'),Membership(org_id=org.id,user_id=member_id,role='member')])
  await db.commit()
  await set_share(db,workspace,org.id,'member',True)
 return {'shared':True}
'''


def verify(store,modes,url=None):
 report=Path(tempfile.mkdtemp(prefix='file-owner-storage-runs-',dir=SERVER.parents[1]/'tmp'))
 database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
 store.raw_client.put_bucket_versioning(Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
 api=IsolatedServer('file-owner-storage',app='file_owner:app',database_url=url)
 settings={**store.settings(),'VIBEFLOW_DATABASE_URL':database}
 (api.root/'file_owner.py').write_text('import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'+INSPECT+OBSERVE+FIXTURE,encoding='utf8')
 checks=[]
 with api:
  case=api.create_case('File owner storage');case_id=case['id'];owner=api.call('GET','/auth/me')['id']
  api.call('POST','/case-provisions:recover')
  endpoint='/api/v1'+api.prefix
  def state():return api.call('GET',f'/fixture/owner/{case_id}')
  prefix=state()['prefix']
  def content(path):return read(store,prefix,path)
  error={'title':'test','stage':'mesh','category':'meshQuality','severity':'error','message':'test','status':'open','blocking':False}
  api.call('PUT',api.prefix+'/caseDict/block',{'block':'errors','entries':{'entries':{'E1':error}}})
  with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=60) as client:
   actions=[
    ('file','PUT','/file',{'json':{'path':'Ref/edit.txt','content':'saved'}},'Ref/edit.txt'),
    ('upload','POST','/upload?path=Ref/upload.bin',{'files':{'file':('upload.bin',b'\x00binary','application/octet-stream')}},'Ref/upload.bin'),
    ('delete','DELETE','/file?path=Ref/upload.bin',{},'Ref/upload.bin'),
    ('dictionary','PUT','/caseDict',{'json':{'content':content('0-caseDict/caseDict').decode()}},'0-caseDict/caseDict'),
    ('block','PUT','/caseDict/block',{'json':{'block':'intent','entries':{'userIntent':'owner quota'}}},'0-caseDict/caseDict'),
    ('extension','PUT','/caseDict/extensions/quota',{'json':{'version':'1','enabled':True,'settings':{}}},'0-caseDict/caseDict'),
    ('extension_delete','DELETE','/caseDict/extensions/quota',{},'0-caseDict/caseDict'),
    ('error','PATCH','/caseDict/errors/E1',{'json':{'status':'verified','fixOptions':{'text':'repair'},'resolution':'fixed','verification':'checked'}},'0-caseDict/caseDict')]
   if 'endpoints' in modes:
    for name,method,suffix,body,path in actions:
     before=content(path);used=state()['usage']['storage_bytes_used']
     (api.root/f'quota-{owner}').write_text('0')
     denied=client.request(method,endpoint+suffix,**body)
     assert denied.status_code==429,(name,denied.status_code,denied.text)
     assert content(path)==before and state()['usage']['storage_bytes_used']==used,name
     (api.root/f'quota-{owner}').write_text('4')
     accepted=client.request(method,endpoint+suffix,**body)
     assert accepted.status_code==200,(name,accepted.status_code,accepted.text)
     observed=state();assert all(r['state']=='settled' for r in observed['rows']),observed
     assert observed['rows'][-1]['receipt']['used']>0,name
     assert content('.vibeflow/file-transactions/pending.json') is None
     checks.append(name+'_quota_and_settlement');print('PASS '+checks[-1],flush=True)
   if 'preview' in modes:
    result=api.put('A-geo/geometry.py','print("preview")')
    assert result['auto_preview_job']==731 and (api.root/'preview-ok').exists()
    assert all(r['state']=='settled' for r in state()['rows'])
    checks.append('preview_after_credit_close');print('PASS '+checks[-1],flush=True)
   if 'pressure' in modes:
    api.put('Ref/pressure.txt','old')
    used=state()['usage']['storage_bytes_used'];available=64*1024
    store.raw_client.put_object(Bucket=store.bucket,Key=f'workspaces/{owner}/.pressure-filler',Body=b'f'*(4*1024*1024-used-available),IfNoneMatch='*')
    denied=client.put(endpoint+'/file',json={'path':'Ref/pressure.txt','content':'x'*(32*1024)})
    assert denied.status_code==429,(denied.status_code,denied.text)
    observed=state();assert observed['usage']['storage_bytes_used']<=4*1024*1024,observed
    (api.root/f'quota-{owner}').write_text('8')
    api.call('POST','/case-provisions:maintenance')
    assert content('Ref/pressure.txt')==b'old'
    assert content('.vibeflow/file-transactions/pending.json') is None
    assert all(r['state']=='settled' for r in state()['rows'])
    checks.append('physical_peak_refused_and_recoverable');print('PASS '+checks[-1],flush=True)
   if 'shared' in modes:
    registered=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Shared writer'})
    owner_token=api.token;api.token=registered['token'];member=api.call('GET','/auth/me')['id'];api.token=owner_token
    api.call('POST',f'/fixture/share/{case_id}/{member}')
    (api.root/f'quota-{member}').write_text('0');(api.root/f'quota-{owner}').write_text('8')
    before=len(state()['rows']);api.token=registered['token']
    api.put('Ref/shared.txt','member saved')
    assert content('Ref/shared.txt')==b'member saved' and state()['rows']==[]
    (api.root/f'quota-{owner}').write_text('0')
    api.call('PUT',api.prefix+'/file',{'path':'Ref/shared.txt','content':'blocked'},expect=429)
    assert content('Ref/shared.txt')==b'member saved'
    api.token=owner_token;observed=state()
    assert len(observed['rows'])>=before+2 and all(r['state']=='settled' for r in observed['rows'])
    checks.append('shared_member_charges_workspace_owner');print('PASS '+checks[-1],flush=True)
 result={'passed':True,'count':len(checks),'checks':checks,'api':str(api.root)}
 (report/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
 return result


async def main():
 parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
 modes=args.modes or ['endpoints','preview','pressure','shared']
 from s3testsupport import ObjectStore
 from s3_typed_publication_smoke import RUNTIME
 report=Path(tempfile.mkdtemp(prefix='file-owner-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
 with ObjectStore(executable=RUNTIME) as store:results={'sqlite':await asyncio.to_thread(verify,store,modes)}
 if args.postgres:
  import asyncpg
  from sqlalchemy.engine import make_url
  from app.config import Settings
  from workflow_migration_smoke import alembic
  url=make_url(Settings(_env_file=SERVER/'.env').database_url)
  assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
  name='tmp_vf_fileowner_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_fileowner_[0-9a-f]{32}',name)
  admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
  created=False
  try:
   await admin.execute('CREATE DATABASE "'+name+'"');created=True
   target=url.set(database=name).render_as_string(hide_password=False)
   await asyncio.to_thread(alembic,target,report,'upgrade','head')
   with ObjectStore(executable=RUNTIME) as store:results['postgresql']=await asyncio.to_thread(verify,store,modes,target)
  finally:
   if created:await admin.execute('DROP DATABASE "'+name+'"')
   await admin.close()
  results['postgresql']['temporary_database_removed']=True
 (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
 print('FILE_OWNER_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
