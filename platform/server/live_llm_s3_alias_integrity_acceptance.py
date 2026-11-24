"""Actual S3 alias-plan integrity protocol checks, followed by real model readback."""
import asyncio,json
from live_llm_job_repeat_acceptance import main

FIXTURE=r'''
import asyncio,hashlib,uuid
from types import SimpleNamespace
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.db import get_db
from app.routers.workspaces import get_owned_case
from app import storage
from app.services import publication_objects as bundles,publication_alias_objects as aliases,attempt_objects as objects
from app.services.publication_journal import bundle_manifest
from app.services.latex_artifacts import paths
def alias_probe(prefix,case_id,mode):
 assert storage._use_s3()
 identifier=str(uuid.uuid4());source=Path(__file__).parent/('alias-protocol-'+uuid.uuid4().hex);source.mkdir()
 row=SimpleNamespace(id=identifier,case_id=case_id,attempt_id=str(uuid.uuid4()),kind='latex',generation=1,
  immutable_path='.vibeflow/publications/'+identifier,manifest_sha256='0'*64)
 names=paths('E-report/integrity-'+identifier+'.tex');hidden='Ref/non-alias-'+identifier+'.bin'
 values={names['pdf']:('PROTOCOL_BYTES_'+uuid.uuid4().hex).encode(),names['log']:b'actual protocol log member',hidden:('HIDDEN_BYTES_'+uuid.uuid4().hex).encode()}
 records={}
 for name,data in values.items():
  target=source/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
  records[name]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':False}
 descriptor={'handler':'latex.v1','main':'E-report/integrity-'+identifier+'.tex'}
 retired='C-run/retired-'+identifier+'/0.1/U'
 if mode in {'retired_edit','retired_race'}:
  old=b'old retained native field'
  storage.s3().put_object(**objects.arguments(prefix,retired),Body=old)
  descriptor={'handler':'solve.v1','retired_aliases':{retired:{'size':len(old),'sha256':hashlib.sha256(old).hexdigest(),'executable':False}}}
 manifest=bundle_manifest(row,records,descriptor,prefix=prefix)
 row.manifest_sha256=hashlib.sha256(bundles.encoded(manifest)).hexdigest()
 bundles.prepare(prefix,row,source,manifest)
 def write(name,data):storage.s3().put_object(**objects.arguments(prefix,name),Body=data)
 if mode=='alias_source_changed':write(row.immutable_path+'/files/'+names['pdf'],b'changed alias source')
 elif mode=='unaliased_member_changed':write(row.immutable_path+'/files/'+hidden,b'changed non-alias archive member')
 elif mode=='extra_object':write(row.immutable_path+'/files/Ref/unexpected.bin',b'unexpected')
 elif mode=='manifest_changed':write(row.immutable_path+'/manifest.json',bundles.encoded(manifest)+b' ')
 if mode in {'alias_source_changed','unaliased_member_changed'}:
  try:bundles.verify(prefix,row,manifest)
  except ValueError:pass
  else:raise AssertionError('Parallel immutable verification accepted damaged bytes')
  import threading
  assert not any(t.name.startswith('immutable-object-read') for t in threading.enumerate()),'Failed parallel reads did not drain'
 try:
  digest=aliases.prepare(prefix,row,manifest)
 except (ValueError,OSError,KeyError,TypeError) as error:
  assert mode in {'alias_source_changed','unaliased_member_changed','extra_object','manifest_changed'}
  return {'mode':mode,'refused':True,'reason_type':type(error).__name__,'protocol_only':True,
   'parallel_corruption_refused_and_drained':mode in {'alias_source_changed','unaliased_member_changed'}}
 assert mode in {'normal','idempotent','destination_edit','race_edit','race_unrelated','race_aba','unknown_commit','retired_edit','retired_race'}
 if mode=='idempotent':assert aliases.prepare(prefix,row,manifest)==digest
 if mode=='destination_edit':
  storage.s3().put_object(**objects.arguments(prefix,names['pdf']),Body=b'external live edit')
  try:aliases.apply(prefix,row,digest)
  except ValueError:pass
  else:raise AssertionError('Alias overwrote an external destination edit')
  assert storage.get_bytes(prefix,names['pdf'])==b'external live edit'
  return {'mode':mode,'refused':True,'external_edit_preserved':True,'protocol_only':True}
 if mode in {'race_edit','race_unrelated','race_aba','unknown_commit','retired_edit','retired_race'}:
  from app.services.s3_live_store import Client
  from app.services.s3_case_files import read_version
  client=storage.s3();peer=Client(client.raw);save=client._save;triggered=[False]
  if mode=='retired_edit':peer.put_object(**objects.arguments(prefix,retired),Body=b'external retired edit')
  def racing(*args,**kwargs):
   if not triggered[0]:
    triggered[0]=True
    if mode=='unknown_commit':save(*args,**kwargs);raise TimeoutError('Actual CAS committed before lost-response injection')
    target='Ref/unrelated-'+identifier+'.txt' if mode=='race_unrelated' else retired if mode=='retired_race' else names['pdf']
    peer.put_object(**objects.arguments(prefix,target),Body=b'external retired edit' if mode=='retired_race' else b'external concurrent write')
    if mode=='race_aba':peer.delete_object(**objects.arguments(prefix,target))
   return save(*args,**kwargs)
  if mode!='retired_edit':client._save=racing
  try:
   try:aliases.apply(prefix,row,digest)
   except ValueError:
    if mode in {'retired_edit','retired_race'}:
     assert storage.get_bytes(prefix,retired)==b'external retired edit'
     assert storage.get_bytes(prefix,names['pdf'])==values[names['pdf']] and storage.get_bytes(prefix,names['log'])==values[names['log']]
     return {'mode':mode,'refused':True,'edited_retired_field_preserved':True,'current_aliases_published':True,'protocol_only':True}
    assert mode in {'race_edit','race_aba'}
    assert read_version(prefix,names['log'])[0] is None,'Batch partially published before a rejected CAS'
    assert read_version(prefix,names['pdf'])[0]==(b'external concurrent write' if mode=='race_edit' else None)
    return {'mode':mode,'refused':True,'atomic_conflict_preserved':True,'protocol_only':True}
   except TimeoutError:assert mode=='unknown_commit'
   else:assert mode=='race_unrelated'
  finally:client._save=save
  if mode=='unknown_commit':aliases.apply(prefix,row,digest)
  if mode=='race_unrelated':assert storage.get_bytes(prefix,'Ref/unrelated-'+identifier+'.txt')==b'external concurrent write'
  assert storage.get_bytes(prefix,names['pdf'])==values[names['pdf']] and storage.get_bytes(prefix,names['log'])==values[names['log']]
  return {'mode':mode,'refused':False,'actual_cas_exercised':triggered[0],'protocol_only':True}
 aliases.apply(prefix,row,digest)
 assert storage.get_bytes(prefix,names['pdf'])==values[names['pdf']]
 return {'mode':mode,'refused':False,'alias_bytes_verified':True,'protocol_only':True}
@app.post('/api/v1/test/alias-integrity/{case_id}')
async def probe_alias_integrity(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True);prefix=case.s3_prefix;await db.rollback()
 mode=body['mode'];assert mode in {'normal','idempotent','alias_source_changed','unaliased_member_changed','extra_object','manifest_changed','destination_edit','race_edit','race_unrelated','race_aba','unknown_commit','retired_edit','retired_race'}
 return await asyncio.to_thread(alias_probe,prefix,case_id,mode)
'''


def scenario(api,*,store=None,**_):
    assert store is not None,'This explicit acceptance requires --s3'
    checks=[api.call('POST','/test/alias-integrity/'+str(api.case_id),{'mode':mode}) for mode in (
        'normal','idempotent','alias_source_changed','unaliased_member_changed','extra_object','manifest_changed','destination_edit',
        'race_edit','race_unrelated','race_aba','unknown_commit','retired_edit','retired_race')]
    assert len(checks)==13
    report={'passed':True,'checks':checks,'protocol_checks':13,'actual_native_compilation_claimed':False,
        'real_model_requests':0,'mock_llm_calls':0,'actual_s3':True}
    (api.root/'s3-alias-integrity.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
