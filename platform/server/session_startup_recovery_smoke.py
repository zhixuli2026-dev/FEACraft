"""Actual request/session startup deaths and lost registration acknowledgements."""
import argparse,asyncio,hashlib,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import aiohttp
from sqlalchemy.engine import make_url

from testsupport import IsolatedServer,SERVER
from job_owner_startup_smoke import shared
from job_release_smoke import wait_file
from child_launch_registration_smoke import exited
from app.services.owned_process import probe_native
from app.services.windows_process_scope import ProcessHandle
from workflow_migration_smoke import alembic
from codex_wsl_test_support import NativeStateFixture
from native_request_smoke import CONTROLLED as REQUEST,WAIT
from terminal_execution_smoke import CONTROLLED as TERMINAL
from viz_execution_smoke import CONTROLLED as VIZ,STL
from codex_session_smoke import CONTROLLED as CLI,WORKER
from codex_wsl_session_smoke import NATIVE

PROFILES=(('request','windows'),('request','wsl'),('terminal','cmd'),('terminal','wsl'),
          ('terminal','wsl-sandbox'),('viz','windows'),('cli','windows'),('cli','wsl'))
EXTRA='''
import os
from datetime import timedelta
from fastapi import Depends,HTTPException
from sqlalchemy import select,func
from app.db import SessionLocal
from app.models import NativeRequest,NativeSession,NativeVizSession,NativeCliSession,Job
from app.deps import get_current_user
from app.services import process_identity_native as startup_identity
from app.services.owned_process import OwnedProcess
from app.services.durable_events import now
from app.services import native_request_records,terminal_records,viz_records,codex_records,codex_sessions
startup_model={'request':NativeRequest,'terminal':NativeSession,'viz':NativeVizSession,'cli':NativeCliSession}[STARTUP_KIND]
startup_records={'request':native_request_records,'terminal':terminal_records,'viz':viz_records,'cli':codex_records}[STARTUP_KIND]
from app.services.native_execution import NativeExecutor
startup_execute=NativeExecutor._execute
async def select_request_child(self,entry,*args,**kwargs):
 if STARTUP_KIND=='request' and self.records is native_request_records:entry.owned.startup_test_target=True
 return await startup_execute(self,entry,*args,**kwargs)
NativeExecutor._execute=select_request_child
startup_prepare=OwnedProcess.prepare
async def select_session_root(self,*args,**kwargs):
 if STARTUP_KIND!='request':
  async with SessionLocal() as db:
   self.startup_test_target=await db.scalar(select(startup_model.id).where(startup_model.launch_nonce==self.nonce)) is not None
 return await startup_prepare(self,*args,**kwargs)
OwnedProcess.prepare=select_session_root
startup_preparation=OwnedProcess._preparation
async def observe_preparation(self,stage,runtime,host,native=None):
 await startup_preparation(self,stage,runtime,host,native)
 if not getattr(self,'startup_test_target',False):return
 (root/'startup-entered.json').write_text(json.dumps(self.preparation))
 if (root/'startup-mode').read_text()==stage:
  (root/'startup-held.json').write_text(json.dumps(self.preparation))
  for _ in range(3000):
   if (root/'startup-continue').exists():return
   await asyncio.sleep(.02)
  raise TimeoutError('Session startup fixture barrier')
OwnedProcess._preparation=observe_preparation
startup_release=OwnedProcess.release
async def observe_release(self):
 if getattr(self,'startup_test_target',False):
  with (root/'startup-release.log').open('a') as stream:stream.write(self.nonce+'\\n')
 return await startup_release(self)
OwnedProcess.release=observe_release
startup_register=(codex_sessions.Session.register_root if STARTUP_KIND=='cli' else
 native_request_records.register if STARTUP_KIND=='request' else startup_records.register_root)
async def register_ack(*args,**kwargs):
 result=await startup_register(*args,**kwargs)
 if (root/'startup-mode').read_text()=='ack_lost':raise OSError('Injected acknowledgement loss after actual registration commit')
 return result
if STARTUP_KIND=='cli':codex_sessions.Session.register_root=register_ack
elif STARTUP_KIND=='request':native_request_records.register=register_ack
else:startup_records.register_root=register_ack
async def startup_row(db,identifier,user):
 key=int(identifier) if STARTUP_KIND=='viz' else identifier
 row=await db.get(startup_model,key)
 if row is None or row.created_by!=user.id:raise HTTPException(404,'Unknown fixture session')
 return row
@app.get('/api/v1/fixture/startup/state')
async def startup_state(user=Depends(get_current_user)):
 async with SessionLocal() as db:
  rows=list(await db.scalars(select(startup_model).where(startup_model.created_by==user.id).order_by(startup_model.created_at)))
  fields=('phase','owner_id','owner_identity_json','launch_nonce','profile_json',
          'children_json' if STARTUP_KIND=='request' else 'process_json')
  if STARTUP_KIND!='request':fields+=('stop_json',)
  return {'rows':[{'id':r.id,'public':startup_records.public(r),'raw':{k:getattr(r,k) for k in fields}} for r in rows],
   'count':await db.scalar(select(func.count()).select_from(startup_model)),
   'jobs':await db.scalar(select(func.count()).select_from(Job)),
   'api_birth':startup_identity.observe(os.getpid())}
@app.post('/api/v1/fixture/startup/expire/{identifier}')
async def startup_expire(identifier:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  row=await startup_row(db,identifier,user);row.lease_expires_at=now()-timedelta(seconds=1);await db.commit()
 return {'ok':True}
@app.post('/api/v1/fixture/startup/reclaim/{identifier}')
async def startup_reclaim(identifier:str,user=Depends(get_current_user)):
 from app.services.workflow_state import ExecutionConflict
 async with SessionLocal() as db:
  row=await startup_row(db,identifier,user)
  owner=native_request_records.Owner(row.id,'0'*64,row.owner_id,row.launch_nonce)
  await db.rollback()
 async with SessionLocal() as db:
  try:await native_request_records._current(db,owner,owner.nonce,launch=True)
  except ExecutionConflict as error:raise HTTPException(409,error.code)
  raise AssertionError('Foreign request owner was accepted')
'''


def new_api(kind,runtime,database_url=None,overrides=None):
    api=IsolatedServer('session-startup-'+kind,app='startup_fixture:app',database_url=database_url)
    code={'request':REQUEST,'terminal':TERMINAL,'viz':VIZ,'cli':CLI}[kind]
    if kind=='request':
        pandoc=str(SERVER.parents[1]/'extracted/share/vibeflow/tools/pandoc/pandoc.exe')
        code='PANDOC='+repr(pandoc)+'\n'+code
        (api.root/'request_wait.py').write_text(WAIT,encoding='utf8')
    if kind=='cli':
        worker=WORKER
        if runtime=='wsl':
            code+=NATIVE
            worker=worker.replace("'platformFamily':'windows','platformOs':'windows'", "'platformFamily':'unix','platformOs':'linux'")
        (api.root/'cli.py').write_text(worker,encoding='utf8')
    prefix='import os\nos.environ.update('+repr(overrides or {})+')\nSTARTUP_KIND='+repr(kind)+'\n'
    (api.root/'startup_fixture.py').write_text(prefix+code+EXTRA,encoding='utf8')
    (api.root/'startup-mode').write_text('')
    return api


def state(api):return api.call('GET','/fixture/startup/state')


def stable_rows(value):
    # A live renderer heartbeat may persist its existing activity timestamp.
    # Ownership, registration, phase and cleanup must remain identical.
    return [{**row,'public':{k:v for k,v in row['public'].items() if k!='last_activity'}} for row in value['rows']]


async def terminal_start(api,runtime,identifier=None):
    params={'token':api.token}
    params.update({'session':str(identifier)} if identifier is not None else {'shell':runtime,'case':str(api.case_id)})
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.ws_connect(api.base+'/ws/terminal',params=params) as ws:
            frame=await asyncio.wait_for(ws.receive(),65)
            return json.loads(frame.data) if frame.type==aiohttp.WSMsgType.TEXT else {'transport_closed':True}


def launch(api,kind,runtime,body,*,ack=False):
    if kind=='terminal':return asyncio.run(terminal_start(api,runtime))
    if kind=='request':
        mode='latex_probe' if runtime=='wsl' else 'normal'
        return api.call('POST',f'/fixture/start/{api.case_id}/{mode}',api_prefix=False)
    if kind=='viz':return api.call('POST',api.prefix+'/viz:sessions',{'file_path':'D-post/cube.stl'},expect=500 if ack else 200,raw=ack)
    return api.call('POST','/codex/sessions',body,expect=502 if ack else 200)


def public(api,kind,identifier):
    if kind=='request':
        row=api.call('GET','/fixture/request/'+str(identifier),api_prefix=False);row.pop('result',None);return row
    path={'terminal':'/terminal/sessions/','viz':'/viz:sessions/','cli':'/codex/sessions/'}[kind]
    return api.call('GET',path+str(identifier))


def wait_failed(api):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        value=state(api)
        if value['rows'] and value['rows'][0]['public']['phase']=='failed':return value
        time.sleep(.05)
    raise AssertionError('Failed registration did not persist terminal cleanup')


def verify(database_url=None,*,wsl=False):
    profiles=[p for p in PROFILES if wsl or p[1] in {'windows','cmd'}]
    checks=[];receipts=[];roots=[]
    def passed(name):checks.append(name);print('PASS '+name,flush=True)
    for kind,runtime in profiles:
        native=runtime in {'wsl','wsl-sandbox'}
        for boundary in ('created','native_ready' if native else 'host_ready','ack_lost'):
            with ExitStack() as stack:
                if kind=='cli' and native:stack.enter_context(NativeStateFixture())
                owner=new_api(kind,runtime,database_url);roots.append(str(owner.root));stack.enter_context(owner)
                owner.create_case('Startup '+kind+' '+runtime+' '+boundary)
                body={'case_id':owner.case_id,'runtime':runtime}
                if kind=='cli':body['conversation_id']=owner.call('POST','/conversations',{'case_id':owner.case_id,'permission_mode':'plan'})['id']
                if kind=='viz':owner.put('D-post/cube.stl',STL)
                (owner.root/'startup-mode').write_text(boundary)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    submitted=pool.submit(launch,owner,kind,runtime,body,ack=boundary=='ack_lost')
                    try:
                        if boundary=='ack_lost':
                            reply=submitted.result(timeout=65)
                            if kind=='terminal':assert reply.get('type')=='error',reply
                            value=wait_failed(owner);row=value['rows'][0]['public']
                            assert row['cleanup_verified'] is True
                            assert not (owner.root/'startup-release.log').exists()
                            observed=json.loads((owner.root/'startup-entered.json').read_text());exited(observed['host'])
                            receipts.append({'kind':kind,'runtime':runtime,'boundary':boundary,'state':value})
                            passed(kind+'_'+runtime+'_lost_registration_ack_retains_real_cleanup_without_release')
                            continue
                        wait_file(owner.root/'startup-held.json',seconds=40)
                        observed=json.loads((owner.root/'startup-held.json').read_text());held=state(owner)
                        assert len(held['rows'])==1 and held['jobs']==0
                        item=held['rows'][0];identifier=item['id']
                        assert item['raw']['phase']==('active' if kind=='request' else 'starting')
                        assert item['public']['cleanup_verified'] in (False,None)
                        # A query-only handle keeps this real Windows birth
                        # available through exit and prevents fixture PID reuse.
                        retained=ProcessHandle.query(observed['host']);stack.callback(retained.close)
                        peer=new_api(kind,runtime,database_url,shared(owner));stack.enter_context(peer);peer.token=owner.token
                        live=state(peer)
                        (owner.root/'live-result.json').write_text(json.dumps({'held':held,'peer':live},indent=2),encoding='utf8')
                        assert stable_rows(live)==stable_rows(held) and live['count']==held['count'] and live['jobs']==0
                        owner.proc.kill();owner.proc.wait(timeout=10);exited(held['api_birth']);exited(observed['host'])
                        try:reply=submitted.result(timeout=20)
                        except (OSError,ValueError,aiohttp.ClientError):reply=None
                        if kind=='terminal' and reply is not None:assert reply.get('type')!='ready'
                        if observed['native'] is not None:
                            found=observed['native'];deadline=time.monotonic()+20
                            while time.monotonic()<deadline:
                                gone=asyncio.run(probe_native(found['distribution'],found['process']))
                                if gone['state'] in {'exited','mismatch'}:break
                                time.sleep(.1)
                            else:raise AssertionError('Actual native gate did not retire')
                        # Expiry is a disclosed fixture clock change after real
                        # owner death; it is not presented as physical evidence.
                        peer.call('POST','/fixture/startup/expire/'+str(identifier))
                        after=state(peer);row=public(peer,kind,identifier)
                        assert row['phase']=='recovery_required' and row['cleanup_verified'] in (False,None)
                        assert after['rows'][0]['raw']==item['raw'] and after['count']==held['count'] and after['jobs']==0
                        assert row['ended_at'] is None
                        if kind=='terminal':
                            reply=asyncio.run(terminal_start(peer,runtime,identifier));assert reply.get('type')=='error'
                        elif kind=='cli':peer.call('GET','/codex/sessions/'+str(identifier)+'/account',expect=409)
                        elif kind=='viz':peer.call('DELETE','/viz:sessions/'+str(identifier),expect=409)
                        else:peer.call('POST','/fixture/startup/reclaim/'+str(identifier),expect=409)
                        again=state(peer)
                        assert again['rows']==after['rows'] and again['count']==after['count']
                        assert not (owner.root/'startup-release.log').exists() and not (peer.root/'startup-entered.json').exists()
                        token=peer.token;peer.create_case('Foreign startup reader')
                        assert state(peer)['rows']==[]
                        if kind=='request':peer.call('GET','/fixture/request/'+str(identifier),api_prefix=False,expect=404)
                        else:
                            path={'terminal':'/terminal/sessions/','viz':'/viz:sessions/','cli':'/codex/sessions/'}[kind]
                            peer.call('GET',path+str(identifier),expect=404)
                        peer.token=token
                        receipt={'kind':kind,'runtime':runtime,'boundary':boundary,'held':held,'after':after,
                            'public':row,'preparation':observed,'owner_death_actual':True,'lease_expiry_injected':True}
                        (owner.root/'recovery-result.json').write_text(json.dumps(receipt,indent=2),encoding='utf8');receipts.append(receipt)
                        passed(kind+'_'+runtime+'_'+boundary+'_death_preserves_unknown_history_without_replay')
                    finally:(owner.root/'startup-continue').touch()
    result={'passed':True,'count':len(checks),'checks':checks,'profiles':profiles,'evidence':roots,
        'receipts':receipts,'developer_database_touched':False}
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--wsl',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='session-startup-evidence-',dir=SERVER.parents[1]/'tmp'))
    hashes={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'source.json').write_text(json.dumps(hashes,indent=2),encoding='utf8')
    results={'sqlite':await asyncio.to_thread(verify,wsl=args.wsl)}
    (report/'sqlite-result.json').write_text(json.dumps(results['sqlite'],indent=2),encoding='utf8')
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_sessionstart_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_sessionstart_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target,wsl=args.wsl)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    assert hashes=={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')},'Product sources changed during verification'
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('SESSION_STARTUP_RECOVERY_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
