"""Actual Linux initializer matrix, private tmpfs and real installed CLI."""
FIXTURE=r'''
import asyncio,json,subprocess
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.config import get_settings
from app.db import SessionLocal
from app.models import NativeCliSession,CliInitializationReservation
from app.services import codex_sessions as sessions,codex_records as records
from app.services.owned_process import wsl_path
from app.services.codex_wsl_state import initialization_payload

@app.post('/api/v1/test/native-initialization')
async def native_initialization(user=Depends(get_current_user)):
 root=Path(__file__).parent
 source=Path(sessions.__file__).resolve().parents[2]/'cli_native_initialization_checks.py'
 assert source.is_file()
 results={}
 for kind,account in (('matrix','nobody'),('space','root')):
  destination=root/('native-initializer-'+kind+'.json')
  result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user',account,'--exec',
   '/usr/bin/python3','-I','-S','-u',wsl_path(source),kind,wsl_path(destination)],
   capture_output=True,text=True,encoding='utf8',timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
  assert len(result.stdout)+len(result.stderr)<65536
  assert result.returncode==0,{'mode':kind,'returncode':result.returncode,'stderr':result.stderr[-3000:]}
  results[kind]=json.loads(destination.read_text(encoding='utf8'));assert results[kind]['passed']
 matrix=results['matrix'];space=results['space']
 assert matrix['uid']==65534 and len(matrix['crashes'])==7 and len(matrix['faults'])==7
 assert matrix['legacy_complete']['passed'] and matrix['legacy_complete']['original_owner_bytes_preserved']
 assert space['errno']==28 and space['uid']==65534 and space['filesystem_bytes']==65536
 assert space['private_mount_verified'] and space['private_mount_removed']
 selected=matrix['root'];assert selected.startswith('/tmp/vibeflow-init-recovery-') and '/' not in selected[5:]
 get_settings().codex_wsl_state_root=selected
 # The actual platform inventory also sees the retained, unknown test data in
 # other user namespaces. A fresh platform account starts the real Linux CLI.
 session=sessions.Session(None,None,user.id,runtime='wsl')
 try:
  await session.start();assert session.client.initialize_result['platformOs']=='linux'
 finally:await session.close()
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,session.id);credit=await db.get(CliInitializationReservation,session.id)
  public=records.public(row);assert public['cleanup_verified'] and public['stop']['native_state']=='exited'
  assert credit.state=='settled' and credit.remaining_bytes==0 and json.loads(credit.receipt_json)['status']=='prepared'
  assert credit.size==sum(map(len,initialization_payload(session.native_state)))
  results['actual_cli']={'session':public,'initialization_bytes':credit.size}
 results.update(passed=True,mock_llm_calls=0,cli_model_inference_verified=False)
 (root/'native-initializer-acceptance.json').write_text(json.dumps(results,indent=2),encoding='utf8')
 return results
'''


def verify(api,**_):
    import httpx
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=240,trust_env=False) as client:
        response=client.post('/test/native-initialization');response.raise_for_status();result=response.json()
    assert result['passed']
    print('ACTUAL_NATIVE_INITIALIZATION_PASS '+str(api.root),flush=True)
    return result
