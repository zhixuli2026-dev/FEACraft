"""Real UDF runs with physically absent/conflicting library aliases.

Faults change only this isolated case's mutable alias, never the publication or
an LLM/native response. The original native preparation/execution is called.
"""
import asyncio,json
from pathlib import Path
import live_llm_udf_workflow_acceptance as chain
from live_llm_job_repeat_acceptance import main

FAULT=r'''
import asyncio,hashlib,json,os,re
from app import storage
from app.models import JobAttempt
from app.services import attempt_workspace as workspace
from app.services.artifact_publication import current,read_manifest
from app.services.job_execution import completion_visibility
from app.services.workflow_state import ExecutionConflict
from app.services.workflow_udf_handler import WorkflowUdfHandler

original_pinned_prepare=WorkflowUdfHandler.prepare
original_pinned_before_publish=WorkflowUdfHandler.before_publish

def mutable_fault(prefix,path,expected,mode):
 assert re.fullmatch(r'workspaces/\d+/cases/\d+',prefix)
 before=storage.get_bytes(prefix,path)
 assert hashlib.sha256(before).hexdigest()==expected
 changed=b'ACTUAL_EXTERNAL_LIBRARY_ALIAS_CONFLICT'
 if storage._use_s3():
  from app.config import get_settings
  client=storage.s3();args={'Bucket':get_settings().s3_bucket,'Key':storage._s3_key(prefix,path)}
  etag=client.head_object(**args)['ETag']
  if mode=='missing':client.delete_object(**args,IfMatch=etag)
  else:client.put_object(**args,Body=changed,IfMatch=etag)
 else:
  target=workspace.checked_path(storage._fs_path(prefix,'.'),path)
  public=storage._fs_root().resolve();assert target.resolve().is_relative_to(public)
  if mode=='missing':target.unlink()
  else:target.write_bytes(changed)
 return None if mode=='missing' else hashlib.sha256(changed).hexdigest()

async def pinned_prepare(self,db,case,user,**kwargs):
 if self.mode=='integrate':
  _,publication=await current(db,case.id,'udf-build')
  if publication is not None:
   witness=root/('udf-alias-fault-'+publication.id+'.json')
   if not witness.exists():
    attempt=await db.get(JobAttempt,publication.attempt_id,populate_existing=True)
    if completion_visibility(attempt)!='settled':
     raise ExecutionConflict('validation_busy','Actual build aliases have not settled before the external-writer fault')
    _,manifest=read_manifest(case.s3_prefix,publication)
    path='UDF/'+kwargs['parameters']['name']+'/libmyFunctionObject.so'
    expected=manifest['files'][path]['sha256'];mode='missing' if publication.generation==1 else 'conflict'
    after=await asyncio.to_thread(mutable_fault,case.s3_prefix,path,expected,mode)
    value={'publication_id':publication.id,'case_id':case.id,'path':path,'mode':mode,
     'original_published_library_sha256':expected,'external_alias_sha256':after,
     'actual_mutable_alias_fault':True,'publication_bytes_not_modified':True,'pid':os.getpid()}
    with witness.open('x',encoding='utf8') as stream:json.dump(value,stream,indent=2)
 result=await original_pinned_prepare(self,db,case,user,**kwargs)
 if self.mode=='integrate':
  path='UDF/'+kwargs['parameters']['name']+'/libmyFunctionObject.so'
  refs=result.descriptor['input_publications']
  selected=[root/('udf-alias-fault-'+r['publication_id']+'.json') for r in refs]
  witness=next(p for p in selected if p.exists());value=json.loads(witness.read_text())
  assert result.descriptor['native']['source']['files'][path]['sha256']==value['original_published_library_sha256']
 return result

def pinned_before_publish(self,prefix,descriptor):
 if self.mode=='integrate':
  candidates=[root/('udf-alias-fault-'+r['publication_id']+'.json') for r in descriptor['input_publications']]
  witness=next(p for p in candidates if p.exists());value=json.loads(witness.read_text())
  actual=hashlib.sha256(storage.get_bytes(prefix,value['path'])).hexdigest() if storage.exists(prefix,value['path']) else None
  assert actual==value['external_alias_sha256']
  loaded=descriptor['native']['receipt']['native']['library']['sha256']
  assert loaded==value['original_published_library_sha256']
  proof={**value,'actual_loaded_library_sha256':loaded,'external_alias_unchanged':True,
   'passed':True,'actual_original_UDF_native_receipt_verified':True}
  destination=root/('udf-pinned-proof-'+descriptor['execution_id']+'.json')
  destination.write_text(json.dumps(proof,indent=2),encoding='utf8')
 return original_pinned_before_publish(self,prefix,descriptor)

WorkflowUdfHandler.prepare=pinned_prepare
WorkflowUdfHandler.before_publish=pinned_before_publish
'''

original_peer=chain.peer
def peer(api,store,role):
    value=original_peer(api,store,role)
    with (value.root/'agent_peer_fixture.py').open('a',encoding='utf8') as stream:stream.write(FAULT)
    return value
chain.peer=peer
FIXTURE=chain.FIXTURE+FAULT


def scenario(api,**kwargs):
    (api.root/'pinned-input-entry-source.py').write_bytes(Path(__file__).read_bytes())
    result=chain.scenario(api,**kwargs)
    proofs=[json.loads(p.read_text(encoding='utf8')) for p in api.root.glob('udf-pinned-proof-*.json')]
    assert len(proofs)==2 and {r['mode'] for r in proofs}=={'missing','conflict'}
    assert all(r['passed'] and r['external_alias_unchanged'] for r in proofs)
    report={'passed':True,'backend':result['backend'],'mock_llm_calls':0,'actual_alias_proofs':proofs,
        'actual_native_chain':result,'scope':'Real missing/conflicting mutable aliases; native UDF loads pinned ELF, source alias remains untouched; source dependency closure and running cancellation remain'}
    (api.root/'udf-pinned-inputs.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
