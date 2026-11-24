"""Actual encrypted files, Windows replacement denial and native WSL paths."""
FIXTURE=r'''
import asyncio,ctypes,hashlib,json,os,subprocess,sys,uuid
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.config import get_settings
from app import storage
from app.services import codex_sessions as sessions,node_storage_budget as budget
from app.services.codex_projection import Secrets,MAX_SECRET_STORE
from app.services.cli_private_paths import root as private_root
from app.services.runtime_config import get_runtime_setting
from app.db import SessionLocal
from app.models import NativeCliSession
from app.services import codex_records as records,process_identity_native as identity

@app.post('/api/v1/test/cli-vault-capacity')
async def vault_capacity(body:dict,user=Depends(get_current_user)):
 settings=get_settings();limit=settings.node_storage_limit_mb;minimum=settings.node_storage_min_free_mb
 native=settings.codex_wsl_state_root;root=private_root(settings);filler=None;handle=None;session=None
 evidence={'passed':False,'mock_llm_calls':0,'llm_calls':0,'checks':[]}
 report=Path(__file__).parent/'cli-vault-checks.json'
 def save():report.write_text(json.dumps(evidence,indent=2),encoding='utf8')
 def checked(name,**data):
  evidence['checks'].append({'name':name,'kind':'actual_infrastructure',**data});save()
 def deny(operation,code=None):
  try:operation()
  except Exception as error:
   if code is not None:assert getattr(error,'code',None)==code,type(error).__name__
   else:assert isinstance(error,(ValueError,OSError)),type(error).__name__
  else:raise AssertionError('Private vault operation unexpectedly succeeded')
 def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
 try:
  settings.node_storage_min_free_mb=0
  environment,_,_=await asyncio.to_thread(sessions.private_environment,user.id,str(uuid.uuid4()))
  vault=Secrets(environment['CODEX_HOME'])
  first='PRIVATE_'+uuid.uuid4().hex;second='PRIVATE_'+uuid.uuid4().hex
  filler=root/'vault-capacity.bin';filler.write_bytes(os.urandom(2*1024*1024))
  settings.node_storage_limit_mb=1
  await asyncio.to_thread(deny,lambda:vault.remember([first]),'node_storage_full')
  assert not vault.path.exists() and not vault.key_path.exists()
  checked('initial_over_capacity_refused_before_key_or_ciphertext')
  settings.node_storage_limit_mb=limit;filler.unlink();filler=None
  await asyncio.to_thread(vault.remember,[first])
  assert first.encode() not in vault.path.read_bytes() and Secrets(environment['CODEX_HOME']).load()==[first]
  old=digest(vault.path);old_key=digest(vault.key_path)
  checked('actual_encrypted_write_and_fresh_instance_decryption',ciphertext_bytes=vault.path.stat().st_size)
  filler=root/'vault-capacity.bin';filler.write_bytes(os.urandom(2*1024*1024));settings.node_storage_limit_mb=1
  await asyncio.to_thread(deny,lambda:vault.remember([second]),'node_storage_full')
  assert digest(vault.path)==old and digest(vault.key_path)==old_key and Secrets(environment['CODEX_HOME']).load()==[first]
  checked('replacement_over_capacity_keeps_old_key_and_ciphertext')
  await asyncio.to_thread(vault.remember,[first])
  assert digest(vault.path)==old
  checked('unchanged_secret_merge_does_not_write_or_reserve')
  await asyncio.to_thread(vault.remember,[first]*512)
  assert digest(vault.path)==old
  checked('maximum_approval_batch_deduplicates_before_persistent_count_limit')
  settings.node_storage_limit_mb=limit;filler.unlink();filler=None
  too_large=[str(index)+'\U0001f642'*4092 for index in range(80)]
  await asyncio.to_thread(deny,lambda:vault.remember(too_large))
  assert digest(vault.path)==old and digest(vault.key_path)==old_key
  checked('multibyte_secret_merge_rejected_before_unreadable_ciphertext')
  await asyncio.to_thread(deny,lambda:vault.remember(['x'*4097]))
  assert digest(vault.path)==old
  checked('invalid_secret_length_rejected_without_replacing_store')
  # A real Windows handle permits reads/writes but denies deletion/replacement.
  # No filesystem function or model call is substituted.
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
  kernel.CreateFileW.restype=ctypes.c_void_p
  kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.CloseHandle.restype=ctypes.c_int
  handle=kernel.CreateFileW(str(vault.path),0x80000000,3,None,3,0,None)
  assert handle not in (None,ctypes.c_void_p(-1).value)
  try:await asyncio.to_thread(deny,lambda:vault.remember([second]))
  finally:assert kernel.CloseHandle(handle);handle=None
  assert digest(vault.path)==old and digest(vault.key_path)==old_key and not list(vault.folder.glob('.redaction-*.tmp'))
  assert sorted(vault.folder.glob('.redaction-*'))==[vault.path]
  checked('actual_windows_replace_denial_preserves_old_cache_and_removes_temporary_file')
  await asyncio.to_thread(vault.remember,[second])
  fresh=Secrets(environment['CODEX_HOME']);assert fresh.load()==[first,second]
  cleaned=fresh.clean(first+' / '+second);assert first not in cleaned and second not in cleaned
  checked('successful_retry_merges_and_redacts_both_actual_values')
  # A ciphertext with a missing key is preserved and never silently re-keyed.
  key=vault.key_path.read_bytes();vault.key_path.unlink();cipher=digest(vault.path)
  try:
   await asyncio.to_thread(deny,lambda:Secrets(environment['CODEX_HOME']).load())
   assert not vault.key_path.exists() and digest(vault.path)==cipher
  finally:
   with vault.key_path.open('xb') as stream:stream.write(key)
  checked('missing_key_read_fails_without_regenerating_key')
  outside=Path(__file__).parent/('vault-outside-'+uuid.uuid4().hex);outside.mkdir()
  await asyncio.to_thread(deny,lambda:Secrets(outside/'home').remember([first]))
  assert not list(outside.iterdir())
  checked('vault_write_outside_configured_account_root_refused')
  # Isolated, retained native directory, never the deployment's real WSL root.
  distro=get_runtime_setting('wsl_distro','vibeflow-linux')
  created=await asyncio.to_thread(subprocess.run,['wsl.exe','-d',distro,'-u','nobody','--exec','/usr/bin/python3','-c',
    "import tempfile; print(tempfile.mkdtemp(prefix='vibeflow-cli-vault-',dir='/tmp'))"],
    capture_output=True,text=True,encoding='utf8',timeout=20,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
  selected=created.stdout.strip();assert selected.startswith('/tmp/vibeflow-cli-vault-') and '/' not in selected[len('/tmp/'):]
  settings.codex_wsl_state_root=selected
  native_base=Path('\\\\wsl.localhost\\'+distro+selected.replace('/','\\'))
  native_home=native_base/'users'/str(user.id)/'wsl'/distro/'home'
  native_home.mkdir(parents=True)
  native_vault=Secrets(native_home)
  await asyncio.to_thread(native_vault.remember,[first,second])
  assert Secrets(native_home).load()==[first,second] and first.encode() not in native_vault.path.read_bytes()
  evidence['retained_native_test_root']=selected
  checked('actual_native_wsl_cache_write_and_decryption_with_target_volume_check')
  native_hash=digest(native_vault.path)
  filler=root/'vault-capacity.bin';filler.write_bytes(os.urandom(2*1024*1024));settings.node_storage_limit_mb=1
  await asyncio.to_thread(deny,lambda:native_vault.remember(['PRIVATE_'+uuid.uuid4().hex]),'node_storage_full')
  assert digest(native_vault.path)==native_hash
  checked('native_vault_write_participates_in_host_peak_admission')
  filler.unlink();filler=None;settings.node_storage_limit_mb=limit
  session=sessions.Session(body['case_id'],None,user.id);await session.start();await session.close()
  async with SessionLocal() as db:evidence['session']=records.public(await db.get(NativeCliSession,session.id))
  evidence['actual_exit']=identity.probe(evidence['session']['process']['root']['host'])
  assert evidence['session']['cleanup_verified'] and evidence['actual_exit']['state']=='exited'
  checked('actual_installed_cli_initializes_and_closes_with_populated_secret_cache')
  assert not list((storage._fs_root()/budget.DIRECTORY).glob('*.json'))
  evidence['claims_empty']=True;evidence['passed']=True;save();return evidence
 except BaseException as error:
  evidence['failure_type']=type(error).__name__;save();raise
 finally:
  settings.node_storage_limit_mb=limit;settings.node_storage_min_free_mb=minimum;settings.codex_wsl_state_root=native
  if filler is not None and filler.exists():filler.unlink()
  if handle is not None:kernel.CloseHandle(handle)
  if session is not None and not session.stopping:
   try:await session.close(error='AcceptanceCleanup')
   except Exception:pass
'''


def verify(api,**_):
    import httpx
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      timeout=120,trust_env=False) as client:
        response=client.post('/test/cli-vault-capacity',json={'case_id':api.case_id})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==14 and result['claims_empty']
    print('CLI_VAULT_CAPACITY_PASS '+str(api.root),flush=True)
    return result
