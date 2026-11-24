"""Real private files, OS-lock peers, installed CLI; no simulated model turns."""
FIXTURE=r'''
import asyncio,base64,hashlib,json,math,os,subprocess,sys,time,uuid
from pathlib import Path
from types import SimpleNamespace
from fastapi import Depends
from app.deps import get_current_user
from app.config import get_settings
from app.db import SessionLocal
from app.models import Case,NativeCliSession
from app import storage
from app.services import codex_sessions as sessions,codex_records as records,codex_images as images
from app.services import node_storage_budget as budget,process_identity_native as identity
from app.services.cli_private_paths import root as private_root
from app.services.storage_usage import Counter,_scan_fs_tree

@app.post('/api/v1/test/cli-private-capacity')
async def private_capacity(body:dict,user=Depends(get_current_user)):
 settings=get_settings();limit=settings.node_storage_limit_mb;minimum=settings.node_storage_min_free_mb
 entries=settings.storage_measurement_max_entries
 root=private_root(settings);root.mkdir(parents=True,exist_ok=True)
 evidence={'passed':False,'mock_llm_calls':0,'llm_calls':0,'checks':[]}
 path=Path(__file__).parent/'cli-private-checks.json'
 def save():path.write_text(json.dumps(evidence,indent=2),encoding='utf8')
 def checked(name,**value):
  evidence['checks'].append({'name':name,'kind':'actual_infrastructure',**value});save()
 def deny(operation,code):
  try:operation()
  except Exception as error:
   assert getattr(error,'code',None)==code,(type(error).__name__,getattr(error,'code',None))
  else:raise AssertionError('Actual capacity unexpectedly admitted')
 def admit(size,**kwargs):
  with budget.reserve(size,**kwargs):pass
 def measured():
  counter=Counter(settings,time.monotonic()+30,None)
  for directory in (storage._fs_root().resolve(),root):
   _scan_fs_tree(SimpleNamespace(storage_fs_root=str(directory.parent)),(directory.name,),counter)
  return counter.bytes
 filler=root/('retained-'+uuid.uuid4().hex+'.bin')
 link=root/('link-'+uuid.uuid4().hex);outside=Path(__file__).parent/('outside-'+uuid.uuid4().hex)
 children=[];session=None;publication=None;peer=None
 try:
  async with SessionLocal() as db:case=await db.get(Case,body['case_id']);prefix=case.s3_prefix
  settings.node_storage_min_free_mb=0
  filler.write_bytes(os.urandom(2*1024*1024));filler_hash=hashlib.sha256(filler.read_bytes()).hexdigest()
  settings.node_storage_limit_mb=1
  await asyncio.to_thread(deny,lambda:admit(0),'node_storage_full')
  checked('retained_private_bytes_block_public_node_allocation')
  identifier=str(uuid.uuid4())
  await asyncio.to_thread(deny,lambda:sessions.private_environment(user.id,identifier),'node_storage_full')
  assert not (root/'users'/str(user.id)/'sessions'/identifier).exists() and hashlib.sha256(filler.read_bytes()).hexdigest()==filler_hash
  checked('private_initialization_refused_before_state_write')
  settings.node_storage_limit_mb=limit
  environment,workspace,_=await asyncio.to_thread(sessions.private_environment,user.id,str(uuid.uuid4()))
  selected=SimpleNamespace(input_root=workspace.parent/'inputs',runtime='windows',selected={'source_prefix':prefix})
  # A valid local PNG enters the real validation path; it is an input file, not a model reply.
  png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII=')
  from app.routers.codex import NativeImageIn
  prepared=images.prepare([NativeImageIn(name='capacity.png',mime='image/png',data=base64.b64encode(png).decode())])
  publication=images.Publication(selected,str(uuid.uuid4()),prepared)
  settings.node_storage_limit_mb=1
  await asyncio.to_thread(deny,publication.write,'node_storage_full')
  assert not publication.folder.exists() and not storage.exists(prefix,prepared[0][0]['path'])
  checked('image_copy_refused_before_private_or_public_write')
  filler.unlink();settings.node_storage_limit_mb=limit
  await asyncio.to_thread(publication.write);publication.verify()
  local=publication.files[0][0].read_bytes();remote=storage.get_bytes(prefix,prepared[0][0]['path'])
  assert local==remote==prepared[0][1]
  checked('actual_private_and_public_image_copies_match',bytes=len(local))
  await asyncio.to_thread(publication.rollback)
  assert not publication.folder.exists() and not storage.exists(prefix,prepared[0][0]['path'])
  checked('uncommitted_image_copies_rolled_back')
  outside.mkdir();(outside/'sentinel').write_bytes(b'UNCHANGED_OUTSIDE_PRIVATE_ROOT')
  os.symlink(outside,link,target_is_directory=True)
  try:await asyncio.to_thread(deny,lambda:admit(0),'node_storage_unavailable')
  finally:link.unlink()
  assert (outside/'sentinel').read_bytes()==b'UNCHANGED_OUTSIDE_PRIVATE_ROOT'
  checked('actual_symlink_refused_without_following_target')
  settings.storage_measurement_max_entries=1
  await asyncio.to_thread(deny,lambda:admit(0),'node_storage_unavailable')
  settings.storage_measurement_max_entries=entries
  checked('real_directory_scan_entry_bound_refuses_unknown_usage')
  base=await asyncio.to_thread(measured);peer_limit=math.ceil(base/(1024*1024))+2
  settings.node_storage_limit_mb=peer_limit
  env=dict(os.environ,VIBEFLOW_NODE_STORAGE_LIMIT_MB=str(peer_limit),VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0',
      PYTHONPATH=str(Path(sessions.__file__).parents[2])+os.pathsep+os.environ.get('PYTHONPATH',''))
  source="""import os,sys
from pathlib import Path
from app.services import node_storage_budget as b
from app.services.cli_private_paths import root
from app.config import get_settings
claim=b._admit(1792*1024,private=True)
print('held',flush=True)
action=sys.stdin.readline().strip()
if action=='crash':
 (root(get_settings())/'peer-retained.bin').write_bytes(b'ACTUAL_CRASH_RETAINED')
 os._exit(77)
b._release(*claim)
"""
  async def start_peer():
   process=await asyncio.create_subprocess_exec(sys.executable,'-X','utf8','-B','-c',source,
     stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
     env=env,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
   children.append(process)
   line=await asyncio.wait_for(process.stdout.readline(),15)
   assert line.strip()==b'held','Actual peer admission failed'
   return process
  peer=await start_peer()
  await asyncio.to_thread(deny,lambda:admit(1792*1024,private=True),'node_storage_full')
  peer.stdin.write(b'release\n');await peer.stdin.drain();assert await asyncio.wait_for(peer.wait(),10)==0
  await asyncio.to_thread(admit,1792*1024,private=True)
  checked('actual_peer_claim_blocks_overcommit_then_releases')
  peer=await start_peer();peer_birth=identity.observe(peer.pid)
  peer.stdin.write(b'crash\n');await peer.stdin.drain();assert await asyncio.wait_for(peer.wait(),10)==77
  assert identity.probe(peer_birth)['state']=='exited'
  retained=root/'peer-retained.bin';assert retained.read_bytes()==b'ACTUAL_CRASH_RETAINED'
  await asyncio.to_thread(admit,1792*1024,private=True)
  assert retained.read_bytes()==b'ACTUAL_CRASH_RETAINED'
  checked('exited_peer_claim_reconciled_without_deleting_retained_data')
  settings.node_storage_limit_mb=limit;settings.node_storage_min_free_mb=minimum
  session=sessions.Session(body['case_id'],None,user.id)
  await session.start();evidence['initialize_result']=session.client.initialize_result
  await session.close()
  async with SessionLocal() as db:evidence['session']=records.public(await db.get(NativeCliSession,session.id))
  evidence['actual_host_exit']=identity.probe(evidence['session']['process']['root']['host'])
  assert evidence['session']['cleanup_verified'] and evidence['actual_host_exit']['state']=='exited'
  checked('actual_installed_cli_initializes_and_closes_with_private_admission')
  assert not list((storage._fs_root()/budget.DIRECTORY).glob('*.json'))
  evidence['claims_empty']=True;evidence['passed']=True;save();return evidence
 finally:
  settings.node_storage_limit_mb=limit;settings.node_storage_min_free_mb=minimum;settings.storage_measurement_max_entries=entries
  if filler.exists():filler.unlink()
  if link.is_symlink():link.unlink()
  for process in children:
   if process.returncode is None:
    process.stdin.write(b'release\n')
    try:await process.stdin.drain();await asyncio.wait_for(process.wait(),10)
    except Exception:
     if process.returncode is None:process.kill();await process.wait()
  if session is not None and not session.stopping:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
'''


def verify(api,**_):
    result=api.call('POST','/test/cli-private-capacity',{'case_id':api.case_id})
    assert result['passed'] and len(result['checks'])==10 and result['claims_empty']
    print('CLI_PRIVATE_CAPACITY_PASS '+str(api.root),flush=True)
    return result
