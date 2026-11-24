"""Actual S3 cold/warm publication cache integrity, with real model readback.

The publication metadata and bytes are protocol fixtures, not compiled output.
"""
import asyncio,json
from live_llm_job_repeat_acceptance import main

FIXTURE=r'''
import asyncio,hashlib,threading,uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from fastapi import Depends
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import attempt_objects as objects,publication_objects as publications
from app.services.fs_directory import remove_owned
from app.services.publication_journal import bundle_manifest

def publication_cache_probe(prefix,case_id,mode):
 assert storage._use_s3()
 identifier=str(uuid.uuid4());source=Path(__file__).parent/('cache-input-'+uuid.uuid4().hex);source.mkdir()
 row=SimpleNamespace(id=identifier,case_id=case_id,attempt_id=str(uuid.uuid4()),kind='latex',generation=1,
  immutable_path='.vibeflow/publications/'+identifier,manifest_sha256='0'*64)
 values={'Ref/member-'+str(i)+'.bin':('ACTUAL_PROTOCOL_MEMBER_'+str(i)).encode() for i in range(32)}
 records={}
 for name,data in values.items():
  target=source/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
  target.chmod(0o755 if name.endswith('0.bin') else 0o644)
  records[name]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':bool(target.stat().st_mode&0o111)}
 descriptor={'handler':'latex.v1','main':'E-report/protocol.tex'}
 manifest=bundle_manifest(row,records,descriptor,prefix=prefix)
 row.manifest_sha256=hashlib.sha256(publications.encoded(manifest)).hexdigest()
 publications.prepare(prefix,row,source,manifest)
 root=storage._fs_path(prefix,'.').resolve();target=root/row.immutable_path;parent=root/'.vibeflow/publications'
 selected='Ref/member-0.bin';remote=row.immutable_path+'/files/'+selected
 def write(name,data):storage.s3().put_object(**objects.arguments(prefix,name),Body=data)
 if mode in {'warm_remote_auth','warm_remote_corrupt','local_cache_corrupt'}:
  cached,_=publications.read_manifest(prefix,row);assert cached==target
  if mode=='local_cache_corrupt':(cached/'files'/selected).write_bytes(b'changed local cache')
 if mode in {'cold_remote_corrupt','warm_remote_corrupt'}:write(remote,b'changed remote member')
 if mode=='missing_member':storage.s3().delete_object(**objects.arguments(prefix,remote))
 if mode=='extra_member':write(row.immutable_path+'/files/Ref/extra.bin',b'actual unexpected object')
 streamed=[];stream_lock=threading.Lock();original=objects.output_stream;changed=[False]
 metadata=publications.bound_metadata;installed=[False]
 def peer_metadata(*args,**kwargs):
  value=metadata(*args,**kwargs)
  if not installed[0]:
   installed[0]=True
   publications.read_manifest(prefix,row)
  return value
 @contextmanager
 def observed(*args,**kwargs):
  with stream_lock:
   streamed.append(args[1])
   mutate=mode=='inventory_race' and not changed[0]
   if mutate:changed[0]=True
  with original(*args,**kwargs) as chunks:
   if mutate:write(row.immutable_path+'/files/Ref/concurrent.bin',b'actual concurrent extra object')
   yield chunks
 objects.output_stream=observed;error=None
 if mode=='peer_cache_install':publications.bound_metadata=peer_metadata
 try:
  try:cached,actual=publications.read_manifest(prefix,row)
  except (ValueError,OSError,KeyError,TypeError) as failure:error=type(failure).__name__+': '+str(failure)
 finally:objects.output_stream=original;publications.bound_metadata=metadata
 expected_failure=mode not in {'cold_restore','warm_remote_auth','peer_cache_install'}
 assert bool(error)==expected_failure,(mode,error)
 if not error:
  assert actual==manifest and len(streamed)==len(records)*(2 if mode=='peer_cache_install' else 1) and len(set(streamed))==len(records)
  for name,value in values.items():assert (cached/'files'/name).read_bytes()==value
  assert bool((cached/'files'/selected).stat().st_mode&0o111)==records[selected]['executable']
 if mode in {'cold_remote_corrupt','inventory_race','missing_member','extra_member'}:
  assert not target.exists(),'Invalid remote archive installed a local cache'
 assert not list(parent.glob('.restoring-'+row.id+'-*')),'Failed cache restoration retained a partial stage'
 assert not any(t.name.startswith('immutable-object-read') for t in threading.enumerate()),'Remote read workers did not drain'
 if mode=='warm_remote_corrupt':assert (target/'files'/selected).read_bytes()==values[selected]
 if mode=='local_cache_corrupt':assert (target/'files'/selected).read_bytes()==b'changed local cache'
 if target.exists():
  assert target.resolve().is_relative_to(root) and target.resolve()!=root
  remove_owned(target,root=root)
 return {'mode':mode,'refused':expected_failure,'reason_type':error,'remote_member_streams':len(streamed),
  'expected_members':len(records),'all_workers_drained':True,'partial_stage_removed':True,'protocol_only':True}

@app.post('/api/v1/testing/publication-cache/{case_id}')
async def probe_publication_cache(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True);prefix=case.s3_prefix;await db.rollback()
 mode=body['mode'];assert mode in {'cold_restore','warm_remote_auth','peer_cache_install','cold_remote_corrupt','warm_remote_corrupt','missing_member','extra_member','inventory_race','local_cache_corrupt'}
 return await asyncio.to_thread(publication_cache_probe,prefix,case_id,mode)
'''


def scenario(api,*,store=None,**_):
    assert store is not None,'Actual owned S3 is required'
    checks=[api.call('POST','/testing/publication-cache/'+str(api.case_id),{'mode':mode}) for mode in (
        'cold_restore','warm_remote_auth','peer_cache_install','cold_remote_corrupt','warm_remote_corrupt',
        'missing_member','extra_member','inventory_race','local_cache_corrupt')]
    report={'passed':True,'actual_s3':True,'protocol_only':True,'actual_native_compilation_claimed':False,
        'cold_cache_single_remote_member_pass':True,'warm_cache_still_authenticates_remote':True,
        'checks':checks,'mock_llm_calls':0}
    (api.root/'s3-publication-cache.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
