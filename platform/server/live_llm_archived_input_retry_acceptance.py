"""Actual S3 sealed retry, strict launch refusal and exited-cache recovery."""
import asyncio,hashlib,json,uuid
from pathlib import Path
from live_llm_job_repeat_acceptance import main,put,repeat

FIXTURE=r'''
import asyncio,subprocess,sys
from pathlib import Path
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.deps import get_current_user
from app.models import User,Job,JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import attempt_workspace as archived_workspace,private_cache
from app.services.case_write import CaseWriteConflict
from app.services.fs_directory import remove_owned

@app.post('/api/v1/jobs/{job_id}/testing/strict-inputs')
async def strict_inputs(job_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db,write=True)
 from sqlalchemy import select
 attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
 def check():
  try:archived_workspace.verify_inputs(case.s3_prefix,json.loads(attempt.input_manifest_json)['workspace'])
  except (OSError,ValueError,CaseWriteConflict):return {'strict_launch_refused':True}
  raise AssertionError('Corrupt original cache passed strict input verification')
 return await asyncio.to_thread(check)

@app.post('/api/v1/cases/{case_id}/testing/cache-directory-recovery')
async def cache_directory_recovery(case_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 await get_owned_case(case_id,user,db,write=True)
 def check():
  checks=[]
  code="""
import json,os,sys
from app.services.private_cache import Cache
cache=Cache('.attempt-sources')
cache.directories(['empty/nested','files'])
cache.write('files/value.txt',b'actual cached file')
mode=sys.argv[1]
if mode=='unknown':(cache.root/'unknown').mkdir()
if mode=='changed':(cache.root/'files/value.txt').write_bytes(b'changed actual cached file')
print(json.dumps({'container':str(cache.container),'parent':str(cache.parent)}),flush=True)
os._exit(0)
"""
  for mode in ('known','unknown','changed'):
   child=subprocess.run([sys.executable,'-X','utf8','-B','-c',code,mode],cwd=Path(private_cache.__file__).resolve().parents[2],
       capture_output=True,text=True,encoding='utf8',timeout=30)
   if child.returncode:
    diagnostic=Path(__file__).parent/('cache-child-'+mode+'.stderr')
    diagnostic.write_text(child.stderr,encoding='utf8')
    raise AssertionError('Actual cache child failed; inspect '+diagnostic.name)
   receipt=json.loads(child.stdout);parent=Path(receipt['parent']).resolve();container=Path(receipt['container']).resolve()
   assert container.parent==parent and container.name.startswith('owned-')
   assert parent==private_cache.parent_for('.attempt-sources').resolve()
   if mode=='known':
    result=private_cache._recover(parent,container)
    assert result['status']=='removed' and not container.exists()
    checks.append({'mode':mode,'actual_owner_exited':True,'journaled_empty_directories_removed':True})
   else:
    try:private_cache._recover(parent,container)
    except ValueError:pass
    else:raise AssertionError('Unknown/changed cache was removed')
    assert container.exists()
    if mode=='unknown':assert (container/'data/unknown').is_dir()
    else:assert (container/'data/files/value.txt').read_bytes()==b'changed actual cached file'
    checks.append({'mode':mode,'actual_owner_exited':True,'unknown_or_changed_cache_retained':True})
    remove_owned(container,root=parent)
  return checks
 return await asyncio.to_thread(check)
'''


def scenario(api,*,store=None,**_):
    assert store is not None,'This acceptance requires the actual owned S3 fixture'
    permit=api.root/'actual-retry-permitted'
    before='SEALED_'+uuid.uuid4().hex;current='CURRENT_'+uuid.uuid4().hex
    code="from pathlib import Path\nprint(Path('Ref/value.txt').read_text().strip())\nif not Path("+repr(str(permit))+").exists():raise SystemExit(19)\n"
    put(api,'Ref/retry.py',code);put(api,'Ref/value.txt',before)
    first=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/retry.py'})
    failed=api.wait_job(first['id'],expected='failed')
    assert failed['exit_code']==19 and before in failed['log_backlog'] and failed['attempt']['process_stop']['verified']
    manifest=failed['attempt']['workspace']
    case_root=api.root/'files'/manifest['storage']['prefix']
    assert case_root.resolve().is_relative_to((api.root/'files').resolve())
    sealed=case_root/manifest['inputs_path'];(sealed/'Ref/value.txt').unlink()
    (sealed/'Ref/retry.py').write_bytes(b'ACTUAL_CORRUPT_OLD_SCRIPT')
    unknown=sealed/'unknown-local-evidence.txt';unknown.write_bytes(b'RETAIN_ME')
    disk=sealed.parent/'manifest.json';disk.write_bytes(b'ACTUAL_CORRUPT_OLD_MANIFEST')
    assert api.call('POST','/jobs/'+str(first['id'])+'/testing/strict-inputs',{})['strict_launch_refused']
    put(api,'Ref/value.txt',current);permit.touch()
    option=api.call('GET','/jobs/'+str(first['id'])+'/operations')
    body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
    key=manifest['storage']['prefix']+'/'+manifest['inputs_path']+'/Ref/value.txt'
    try:
        store.client.put_object(Bucket=store.bucket,Key=key,Body=b'ACTUAL_CORRUPT_REMOTE_INPUT')
        refusal=repeat(api,first['id'],body,uuid.uuid4().hex,expect=409)
        assert refusal['detail']['code'] not in {'validation_busy','storage_quota_busy'}
    finally:store.client.put_object(Bucket=store.bucket,Key=key,Body=before.encode())
    retry_key=uuid.uuid4().hex;retried=repeat(api,first['id'],body,retry_key);done=api.wait_job(retried['id'])
    assert before in done['log_backlog'] and current not in done['log_backlog']
    assert done['attempt']['number']==2 and done['attempt']['root_job_id']==first['id']
    assert done['attempt']['workspace']['files']==manifest['files']
    assert done['attempt']['workspace']['files']['Ref/value.txt']['sha256']==hashlib.sha256(before.encode()).hexdigest()
    assert not (sealed/'Ref/value.txt').exists() and (sealed/'Ref/retry.py').read_bytes()==b'ACTUAL_CORRUPT_OLD_SCRIPT'
    assert unknown.read_bytes()==b'RETAIN_ME' and disk.read_bytes()==b'ACTUAL_CORRUPT_OLD_MANIFEST'
    replay=repeat(api,first['id'],body,retry_key);assert replay['id']==done['id'] and not replay['created']
    assert not list((api.root/'files/.attempt-sources').glob('owned-*'))
    (api.root/'archived-input-retry-progress.json').write_text(json.dumps({'actual_retry_completed':True,
        'source_job':first['id'],'retry_job':done['id'],'same_sealed_files':True,'old_partial_cache_preserved':True}),encoding='utf8')
    recovered=api.call('POST',api.prefix+'/testing/cache-directory-recovery',{})
    report={'passed':True,'mock_llm_calls':0,'actual_python_jobs':2,'source_job':first['id'],'retry_job':done['id'],
        'partial_local_cache_preserved':True,'retry_original_archived_bytes_not_current':True,'same_lineage_and_sealed_files':True,
        'strict_original_launch_refused':True,'damaged_remote_input_refused':True,'retry_idempotent':True,
        'independent_view_cleaned':True,'actual_exited_owner_cache_recovery':recovered}
    (api.root/'archived-input-retry.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
