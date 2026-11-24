"""Actual MPI run using pinned mesh despite two physical alias faults.

The fixture restores only its own fault bytes after actual native verification,
so the unchanged D gate can still require an aligned scientific working copy.
"""
import asyncio,json
from pathlib import Path
import live_llm_mpi_workflow_acceptance as chain
from live_llm_job_repeat_acceptance import main

FAULT=r'''
import asyncio,hashlib,json,re
from app import storage
from app.services import attempt_workspace as workspace
from app.services.artifact_publication import current,read_manifest
from app.services.workflow_mpi_handler import WorkflowMpiHandler

original_mesh_prepare=WorkflowMpiHandler.prepare
original_mesh_before=WorkflowMpiHandler.before_publish

def alter_mesh_alias(prefix,path,expected,mode):
 assert re.fullmatch(r'workspaces/\d+/cases/\d+',prefix)
 data=storage.get_bytes(prefix,path);assert hashlib.sha256(data).hexdigest()==expected
 changed=b'ACTUAL_EXTERNAL_MESH_ALIAS_CONFLICT'
 if storage._use_s3():
  from app.config import get_settings
  args={'Bucket':get_settings().s3_bucket,'Key':storage._s3_key(prefix,path)}
  client=storage.s3();etag=client.head_object(**args)['ETag']
  if mode=='missing':client.delete_object(**args,IfMatch=etag)
  else:client.put_object(**args,Body=changed,IfMatch=etag)
 else:
  target=workspace.checked_path(storage._fs_path(prefix,'.'),path)
  assert target.resolve().is_relative_to(storage._fs_root().resolve())
  if mode=='missing':target.unlink()
  else:target.write_bytes(changed)
 return data,None if mode=='missing' else hashlib.sha256(changed).hexdigest()

async def mesh_prepare(self,db,case,user,**kwargs):
 witness=root/'mpi-mesh-alias-faults.json'
 if not witness.exists():
  _,row=await current(db,case.id,'mesh');_,manifest=read_manifest(case.s3_prefix,row)
  base=kwargs['parameters']['run_dir']+'/constant/polyMesh/';records=[]
  for name,mode in [('points','missing'),('faces','conflict')]:
   path=base+name;expected=manifest['files'][path]['sha256']
   data,after=await asyncio.to_thread(alter_mesh_alias,case.s3_prefix,path,expected,mode)
   backup=root/('original-mesh-'+row.id+'-'+name);backup.write_bytes(data)
   records.append({'path':path,'mode':mode,'original_sha256':expected,'fault_sha256':after,'owned_backup':str(backup)})
  witness.write_text(json.dumps({'publication_id':row.id,'case_id':case.id,'records':records},indent=2),encoding='utf8')
 result=await original_mesh_prepare(self,db,case,user,**kwargs)
 value=json.loads(witness.read_text())
 assert all(result.descriptor['native']['source']['files'][r['path']]['sha256']==r['original_sha256'] for r in value['records'])
 return result

def mesh_before(self,prefix,descriptor):
 value=json.loads((root/'mpi-mesh-alias-faults.json').read_text())
 for row in value['records']:
  actual=hashlib.sha256(storage.get_bytes(prefix,row['path'])).hexdigest() if storage.exists(prefix,row['path']) else None
  assert actual==row['fault_sha256']
  assert descriptor['native']['source']['files'][row['path']]['sha256']==row['original_sha256']
 result=original_mesh_before(self,prefix,descriptor)
 # Restore only the isolated faults created above. No product code repairs
 # aliases during preparation; the ordinary confirmation gate is preserved.
 for row in value['records']:
  data=Path(row['owned_backup']).read_bytes();assert hashlib.sha256(data).hexdigest()==row['original_sha256']
  if storage._use_s3():
   from app.config import get_settings
   client=storage.s3();args={'Bucket':get_settings().s3_bucket,'Key':storage._s3_key(prefix,row['path'])}
   condition={'IfNoneMatch':'*'} if row['mode']=='missing' else {'IfMatch':client.head_object(**args)['ETag']}
   client.put_object(**args,Body=data,**condition)
  else:
   target=storage._fs_path(prefix,row['path']);assert target.resolve().is_relative_to(storage._fs_root().resolve())
   if row['mode']=='missing':
    with target.open('xb') as stream:stream.write(data)
   else:target.write_bytes(data)
  assert hashlib.sha256(storage.get_bytes(prefix,row['path'])).hexdigest()==row['original_sha256']
 proof={'passed':True,'records':value['records'],'actual_native_MPI_verified':True,
  'missing_and_conflicting_aliases_not_used':True,'only_owned_test_faults_restored_for_unchanged_D_gate':True}
 (root/'mpi-pinned-mesh-proof.json').write_text(json.dumps(proof,indent=2),encoding='utf8')
 return result

WorkflowMpiHandler.prepare=mesh_prepare
WorkflowMpiHandler.before_publish=mesh_before
'''
original_peer=chain.peer
def peer(api,store,role):
    value=original_peer(api,store,role)
    with (value.root/'agent_peer_fixture.py').open('a',encoding='utf8') as stream:stream.write(FAULT)
    return value
chain.peer=peer
FIXTURE=chain.FIXTURE+FAULT


def scenario(api,**kwargs):
    (api.root/'pinned-mesh-entry-source.py').write_bytes(Path(__file__).read_bytes())
    value=chain.scenario(api,**kwargs)
    proof=json.loads((api.root/'mpi-pinned-mesh-proof.json').read_text(encoding='utf8'));assert proof['passed']
    result={'passed':True,'backend':value['backend'],'mock_llm_calls':0,'actual_mesh_fault_proof':proof,'actual_native_chain':value}
    (api.root/'mpi-pinned-inputs.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
