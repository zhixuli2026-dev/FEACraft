"""Terminate real APIs at native startup boundaries; never synthesize inference."""
FIXTURE = r'''
import asyncio,hashlib,json,subprocess,time
from pathlib import Path
from fastapi import Depends
from app.deps import get_current_user
from app.db import SessionLocal
from app.config import get_settings
from app.models import NativeCliSession,CliInitializationReservation
from app.services import codex_sessions as sessions,codex_records as records
from app.services import cli_initialization_capacity as capacity,cli_node_capacity as node_capacity
from app.services import node_storage_budget as node_budget,process_identity_native as identity
from app.services.owned_process import probe_native

test_root=Path(__file__).parent
native_config=test_root/'recovery-native-root.json'
if native_config.exists():get_settings().codex_wsl_state_root=json.loads(native_config.read_text(encoding='utf8'))['root']
startup_tasks=set()

async def capture(session,stage):
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,session.id);credit=await db.get(CliInitializationReservation,session.id)
  ticket=capacity.ticket(credit,row)
  report={'stage':stage,'session_id':session.id,'user_id':session.user_id,'owner':json.loads(row.owner_identity_json),
   'journal':records.document(row)[0],'process_json':row.process_json,'stop_json':row.stop_json,'phase':row.phase,
   'credit_state':credit.state,'credit_remaining':credit.remaining_bytes,'receipt_json':credit.receipt_json,
   'credit_binding':json.loads(credit.binding_json),
   'ticket':ticket,'home':session.selected['auth_home'],'auth_config_sha256':session.selected['auth_config_sha256'],
   'transport_started':session.client.transport.started,'mock_llm_calls':0}
 owned=session.client.transport.owned
 report['actual_preparation']=owned.preparation
 report['observed_root']=owned.record
 report['observed_bridge']=session.client.transport.bridge_birth
 root,identifier,encoded=node_capacity.encoded(ticket)
 claim=root/node_budget.DIRECTORY/(identifier+'.json')
 report['claim_sha256']=hashlib.sha256(claim.read_bytes()).hexdigest() if claim.exists() else None
 report['home_exists']=Path(report['home']).exists()
 assert report['stop_json']=='{}'
 if stage not in {'committed','active'}:
  assert report['credit_state']=='active' and report['credit_remaining']==ticket['bytes']
  assert report['claim_sha256']==hashlib.sha256(encoded).hexdigest()
 if stage not in {'prepared','committed','active'}:assert not report['home_exists']
 else:
  assert report['home_exists']
  home=Path(report['home'])
  report['file_hashes']={str(path):hashlib.sha256(path.read_bytes()).hexdigest()
   for path in (home/'config.toml',home.parent/'owner.json')}
 if stage=='committed':assert report['credit_state']=='settled' and report['claim_sha256'] is not None
 if stage=='active':assert report['phase']=='active' and report['transport_started'] and report['claim_sha256'] is None
 (test_root/('crash-'+stage+'.json')).write_text(json.dumps(report,indent=2),encoding='utf8')
 return report

@app.post('/api/v1/test/initializer-crash/{stage}')
async def start_crash(stage:str,user=Depends(get_current_user)):
 assert stage in {'prepare_only','host_created','native_ready','bridge_unregistered','bridge_commit','root','bridge','prepared','committed','active'}
 if not native_config.exists():
  result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','-u','nobody','--exec','/usr/bin/python3','-c',
   "import tempfile; print(tempfile.mkdtemp(prefix='vibeflow-cli-recovery-',dir='/tmp'))"],
   capture_output=True,text=True,encoding='utf8',check=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
  selected=result.stdout.strip();assert selected.startswith('/tmp/vibeflow-cli-recovery-') and '/' not in selected[5:]
  get_settings().codex_wsl_state_root=selected
  native_config.write_text(json.dumps({'root':selected}),encoding='utf8')
 session=sessions.Session(None,None,user.id,runtime='wsl')
 ready=asyncio.Event();release=asyncio.Event();report={}
 async def pause():
  report.update(await capture(session,stage));ready.set();await release.wait()
 if stage=='bridge_commit':
  from sqlalchemy.ext.asyncio import AsyncSession
  original_commit=AsyncSession.commit
  async def held_commit(db):
   row=await db.get(NativeCliSession,session.id)
   if row is not None and row.phase=='starting' and json.loads(row.process_json).get('bridge') is not None:
    # The real bridge registration and credit validation have run, but their
    # SQL transaction has not committed. An API crash must roll it back.
    await pause()
   await original_commit(db)
  AsyncSession.commit=held_commit
 elif stage in {'prepare_only','host_created','native_ready'}:
  original=session.prepare
  async def preparing():
   await original()
   if stage=='prepare_only':await pause()
   else:
    async def created(value):
     if value['stage']==('created' if stage=='host_created' else 'native_ready'):await pause()
    session.client.transport.owned.on_preparation=created
  session.prepare=preparing
 elif stage=='bridge_unregistered':
  original=session.register_bridge
  async def unregistered(value):await pause();await original(value)
  session.register_bridge=unregistered
 elif stage=='root':
  original=session.register_root
  async def registered(value):await original(value);await pause()
  session.register_root=registered
 elif stage=='bridge':
  original=session.register_bridge
  async def registered(value):await original(value);await pause()
  session.register_bridge=registered
 elif stage=='prepared':
  original=session.state_prepared
  async def prepared():await pause();await original()
  session.state_prepared=prepared
 elif stage=='committed':
  original=capacity.closed
  async def closed(user_id):
   if user_id==user.id and session.client.transport.native_state_prepared:await pause()
   await original(user_id)
  capacity.closed=closed
 async def start():
  try:
   await session.start()
   if stage=='active':report.update(await capture(session,stage));ready.set()
  except BaseException as error:
   report['failure_type']=type(error).__name__;ready.set()
 task=asyncio.create_task(start());startup_tasks.add(task);task.add_done_callback(startup_tasks.discard)
 try:
  await asyncio.wait_for(ready.wait(),40)
  assert 'failure_type' not in report,report
  return report
 except BaseException:
  release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);raise

@app.post('/api/v1/test/initializer-after-crash/{stage}')
async def after_crash(stage:str,user=Depends(get_current_user)):
 path=test_root/('crash-'+stage+'.json');report=json.loads(path.read_text(encoding='utf8'))
 assert report['user_id']==user.id
 observations={'owner':identity.probe(report['owner'])}
 assert observations['owner']['state']=='exited'
 root=report['observed_root']
 preparation=report['actual_preparation']
 if preparation is not None:observations['host']=identity.probe(preparation['host'])
 if root is not None:
  observations['host']=identity.probe(root['host'])
  observations['native']=await probe_native(root['native']['distribution'],root['native']['process'])
 if report['observed_bridge'] is not None:
  observations['bridge']=await probe_native(root['native']['distribution'],report['observed_bridge'])
 # Preserve observations even if an actual descendant remains alive.
 report['exit_observations']=observations
 path.write_text(json.dumps(report,indent=2),encoding='utf8')
 assert all(value['state']=='exited' for value in observations.values()),observations
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,report['session_id']);credit=await db.get(CliInitializationReservation,report['session_id'])
  assert row.stop_json==report['stop_json'] and row.process_json==report['process_json']
  assert credit.state==report['credit_state'] and credit.remaining_bytes==report['credit_remaining']
  assert credit.receipt_json==report['receipt_json']
 assert Path(report['home']).exists()==report['home_exists']
 for filename,digest in report.get('file_hashes',{}).items():assert hashlib.sha256(Path(filename).read_bytes()).hexdigest()==digest
 return report

@app.get('/api/v1/test/initializer-final/{stage}')
async def final_crash(stage:str,user=Depends(get_current_user)):
 report=json.loads((test_root/('crash-'+stage+'.json')).read_text(encoding='utf8'));assert report['user_id']==user.id
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,report['session_id']);credit=await db.get(CliInitializationReservation,report['session_id'])
  assert row.stop_json==report['stop_json'] and row.process_json==report['process_json'] and row.phase==report['phase']
  assert credit.state=='settled' and credit.remaining_bytes==0
  result={'session':records.public(row),'receipt':json.loads(credit.receipt_json),'credit_state':credit.state,
   'node_claim_pending':await asyncio.to_thread(node_capacity.outstanding,capacity.ticket(credit,row))}
 assert result['session']['cleanup_verified'] is None
 assert not result['node_claim_pending']
 for filename,digest in report.get('file_hashes',{}).items():assert hashlib.sha256(Path(filename).read_bytes()).hexdigest()==digest
 return result
'''


STAGES = ('prepare_only', 'host_created', 'native_ready', 'root', 'bridge_unregistered',
          'bridge_commit', 'bridge', 'prepared', 'committed', 'active')


def verify(api, *, stages=None, **_):
    import concurrent.futures
    import contextlib
    import httpx
    import json
    import os
    import shutil
    import socket
    import subprocess
    import sys
    import time
    from testsupport import SERVER

    evidence = {'passed': False, 'mock_llm_calls': 0, 'stages': [], 'checks': []}
    path = api.root / 'cli-initialization-recovery-checks.json'

    def save():
        path.write_text(json.dumps(evidence, indent=2), encoding='utf8')

    def checked(name, stage):
        evidence['checks'].append({'name': name, 'stage': stage, 'kind': 'actual_infrastructure'})
        save()

    @contextlib.contextmanager
    def peer(stage):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        assert port != 8000
        base = 'http://127.0.0.1:'+str(port)
        with (api.root/('peer-'+stage+'.log')).open('w', encoding='utf8') as log:
            process = subprocess.Popen([sys.executable, '-B', '-m', 'uvicorn', 'live_fixture:app',
                '--app-dir', str(SERVER), '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
                cwd=api.root, env=dict(api.env), stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                with httpx.Client(trust_env=False, timeout=1) as client:
                    for _ in range(150):
                        assert process.poll() is None, 'Independent recovery API exited during startup'
                        try:
                            if client.get(base+'/api/health').json().get('ok'):break
                        except (httpx.HTTPError, ValueError):pass
                        time.sleep(.1)
                    else:raise TimeoutError('Independent recovery API did not start')
                yield base
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                evidence.setdefault('peers', []).append({'stage':stage,'pid':process.pid,
                    'api':base,'exited':process.poll() is not None})
                save()

    selected=tuple(stages) if stages is not None else STAGES
    assert selected and len(set(selected))==len(selected) and set(selected)<=set(STAGES)
    for stage in selected:
        api.create_case('Native initializer crash ' + stage)
        user = api.call('GET', '/auth/me')
        with httpx.Client(base_url=api.base+'/api/v1', headers={'Authorization': 'Bearer '+api.token},
                          timeout=60, trust_env=False) as client:
            response = client.post('/test/initializer-crash/'+stage)
            response.raise_for_status()
            before = response.json()
        route = '/codex/sessions/'+before['session_id']+':reconcile-initialization'
        if stage not in {'committed','active'}:
            refused = api.call('POST', route, expect=409)
            assert refused['detail']['code'] == 'recovery_required'
            checked('live_owner_or_missing_bridge_refuses_credit_recovery', stage)
        # Kill the actual API, including its background startup task. No stop
        # callback, API shutdown handler or synthetic receipt is substituted.
        api.__exit__(RuntimeError, None, None)
        assert api.proc.poll() is not None
        shutil.copyfile(api.root/'api.log', api.root/('api-crash-'+stage+'.log'))
        api.__enter__()
        api.token = api.call('POST', '/auth/login', {'email': user['email'], 'password': 'isolated-test-123'})['token']
        after = api.call('POST', '/test/initializer-after-crash/'+stage)
        checked('actual_owner_root_and_registered_bridge_exit_preserves_journals', stage)
        # A separate real account must not inspect or recover someone else's
        # native initialization credit, even when its identifier is known.
        token = api.token
        api.create_case('Unauthorized initializer recovery '+stage)
        api.call('POST', route, expect=404)
        api.token = token
        def recover(base):
            with httpx.Client(base_url=base+'/api/v1', headers={'Authorization': 'Bearer '+token},
                              timeout=90, trust_env=False) as client:
                statuses = []
                for attempt in range(5):
                    response = client.post(route)
                    statuses.append(response.status_code)
                    if response.status_code == 409 and response.json().get('detail', {}).get('code') == 'storage_quota_busy':
                        time.sleep(.1)
                        continue
                    response.raise_for_status()
                    evidence.setdefault('worker_requests', []).append({'stage':stage,'api':base,'statuses':statuses})
                    return response.json()
                raise AssertionError('Recovery owner lock did not become available')
        # Distinct actual API processes share only the isolated DB/storage.
        # No in-memory lock can serialize their recovery operations.
        with peer(stage) as other:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(recover, base) for base in (api.base, other)]
                recovered, repeated = [future.result() for future in futures]
        assert recovered['receipt'] == repeated['receipt']
        assert recovered['state'] == 'settled' and recovered['remaining_bytes'] == 0
        assert not recovered['node_claim_pending'] and recovered['session_cleanup_verified'] is None
        if stage in {'bridge', 'prepared'}:
            assert recovered['receipt']['status'] == 'initializer_exited'
        elif stage in {'committed','active'}:
            assert recovered['receipt']['status'] == 'prepared'
        else:
            assert recovered['receipt']['status'] == 'initializer_not_released'
        assert api.call('POST', route)['receipt'] == recovered['receipt']
        checked('two_api_workers_recover_with_one_durable_receipt', stage)
        final = api.call('GET', '/test/initializer-final/'+stage)
        evidence['stages'].append({'before': before, 'after': after, 'recovered': recovered, 'final': final})
        checked('private_files_and_original_stop_journal_retained', stage)
        print('CLI_INITIALIZER_RECOVERY_PASS '+stage, flush=True)
    # Use a fresh ordinary case for the separately reported real Agent proof.
    api.create_case('Initializer recovery actual model proof')
    evidence['passed'] = True
    save()
    return evidence
