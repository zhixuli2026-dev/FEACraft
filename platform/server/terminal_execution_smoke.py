"""Real owned PTYs, durable user sessions and faults on disposable databases."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

import aiohttp
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
from app.services.owned_terminal import PROBE
from mpi_scope_smoke import inspect_native

CONTROLLED='''import asyncio,contextvars,copy,json,os
from pathlib import Path
from datetime import timedelta
from fastapi import Depends,HTTPException
from sqlalchemy import select,update,func
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import NativeSession,Job,User
from app.routers import terminal
from app.services import terminal_records as records
from app.services.owned_terminal import OwnedTerminal
from app.services.durable_events import now
root=Path(__file__).parent;mode='normal';settings={};nonces={};tasks={};transports={};foreign={}
terminal.REAPER_INTERVAL_S=.05
failure=contextvars.ContextVar('terminal_commit_fault',default=False)
commit=AsyncSession.commit
async def committing(db):
 if failure.get():raise OSError('isolated terminal commit failure')
 return await commit(db)
AsyncSession.commit=committing
create=records.create
async def creating(*args,**kwargs):
 owner=await create(*args,**kwargs);settings[owner.id]=mode;nonces[owner.nonce]=mode;tasks[owner.id]=asyncio.current_task()
 return owner
records.create=creating
register_root=records.register_root
async def root_register(owner,birth):
 tasks[owner.id]=asyncio.current_task()
 selected=settings[owner.id]
 if selected=='bad_root':
  birth=copy.deepcopy(birth);birth['host']['creation_time_100ns']=str(int(birth['host']['creation_time_100ns'])+1)
 token=failure.set(selected=='root_commit')
 try:return await register_root(owner,birth)
 finally:failure.reset(token)
records.register_root=root_register
register_gate=records.register_gate
async def gate_register(owner,transport):
 selected=settings[owner.id];transports[owner.id]=transport
 if selected in {'hold','cancel_startup'}:
  marker=root/('gated-'+owner.id+'.json');temporary=marker.with_suffix('.writing')
  temporary.write_text(json.dumps(transport.gate_birth));temporary.replace(marker)
  while not (root/('release-'+owner.id)).exists():await asyncio.sleep(.02)
 if selected=='bad_gate':
  transport.gate_birth=copy.deepcopy(transport.gate_birth);birth=transport.gate_birth['process']
  key='creation_time_100ns' if birth['platform']=='windows' else 'start_ticks';birth[key]=str(int(birth[key])+1)
 if selected=='foreign_gate':transport.gate_birth={**transport.gate_birth,'process':foreign['windows' if transport.distro is None else 'linux']}
 if selected=='gate_profile':transport.gate_birth={**transport.gate_birth,'profile_sha256':'0'*64}
 if selected=='expired_lease':
  async with SessionLocal() as db:
   await db.execute(update(NativeSession).where(NativeSession.id==owner.id).values(lease_expires_at=now()+timedelta(seconds=1)));await db.commit()
 token=failure.set(selected=='gate_commit')
 try:return await register_gate(owner,transport)
 finally:failure.reset(token)
records.register_gate=gate_register
verify=OwnedTerminal.verify_gate
async def verifying(self):
 if nonces.get(self.owned.nonce)=='expired_lease':await asyncio.sleep(1.3)
 return await verify(self)
OwnedTerminal.verify_gate=verifying
release=OwnedTerminal.release
async def releasing(self):
 if nonces.get(self.owned.nonce)=='release_failure':raise OSError('isolated terminal release failure')
 return await release(self)
OwnedTerminal.release=releasing
stop=OwnedTerminal.stop
async def stopping(self,*args):
 value=await stop(self,*args)
 if nonces.get(self.owned.nonce)=='unverified_stop':value['verified']=False;value['error']='OSError'
 return value
OwnedTerminal.stop=stopping
heartbeat=records.heartbeat
async def beating(owner):
 if settings.get(owner.id)=='heartbeat_failure' and (root/('heartbeat-armed-'+owner.id)).exists():
  raise OSError('isolated terminal heartbeat failure')
 return await heartbeat(owner)
records.heartbeat=beating
complete=records.complete
async def completing(owner,*args,**kwargs):
 token=failure.set(settings.get(owner.id)=='receipt_failure')
 try:return await complete(owner,*args,**kwargs)
 finally:failure.reset(token)
records.complete=completing

@app.post('/fixture/mode/{selected}')
async def select_mode(selected:str,body:dict={},user=Depends(get_current_user)):
 global mode,foreign
 mode=selected
 if body:foreign=body
 terminal.IDLE_TIMEOUT_S=.15 if selected=='idle' else 900
 return {'ok':True}

@app.post('/fixture/heartbeat-failure/{identifier}')
async def arm_heartbeat(identifier:str,user=Depends(get_current_user)):
 assert settings.get(identifier)=='heartbeat_failure'
 (root/('heartbeat-armed-'+identifier)).touch()
 return {'armed':True}

@app.get('/fixture/state')
async def state(user=Depends(get_current_user),db=Depends(get_db)):
 rows=(await db.scalars(select(NativeSession).where(NativeSession.created_by==user.id).order_by(NativeSession.created_at))).all()
 return {'sessions':[records.public(row) for row in rows],'jobs':await db.scalar(select(func.count()).select_from(Job))}

@app.post('/fixture/cancel-startup/{identifier}')
async def cancel_startup(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeSession,identifier)
 if row is None or row.created_by!=user.id:raise HTTPException(404)
 await db.rollback();tasks[identifier].cancel();await asyncio.gather(tasks[identifier],return_exceptions=True)
 return {'ok':True}

@app.get('/fixture/members/{identifier}')
async def members(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 import ctypes
 from ctypes import wintypes
 row=await db.get(NativeSession,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 transport=transports[identifier];scope=transport.owned.scope
 kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
 kernel.QueryFullProcessImageNameW.restype=wintypes.BOOL
 result=[]
 with scope.observer.lock:
  for handle in scope.observer.pending.values():
   try:birth=handle.identity()
   except ProcessLookupError:continue
   text=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(text))
   if not kernel.QueryFullProcessImageNameW(handle.handle,0,text,ctypes.byref(size)):raise ctypes.WinError(ctypes.get_last_error())
   result.append({'identity':birth,'name':Path(text.value).name.lower()})
 return {'members':result}

@app.post('/fixture/expire/{identifier}')
async def expire(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeSession,identifier)
 if row is None or row.created_by!=user.id:raise HTTPException(404)
 await db.rollback()
 async with SessionLocal() as current:
  await current.execute(update(NativeSession).where(NativeSession.id==identifier).values(lease_expires_at=now()-timedelta(seconds=1)));await current.commit()
 return {'ok':True}

@app.get('/fixture/journal/{identifier}')
async def journal(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 from types import SimpleNamespace
 row=await db.get(NativeSession,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 original={col.name:getattr(row,col.name) for col in NativeSession.__table__.columns};checked=[]
 for label in ('nonce','runtime','gate_profile','gate_birth','stop_nonce','tail_limit'):
  values=dict(original);document=json.loads(values['process_json']);stop=json.loads(values['stop_json'])
  if label=='nonce':document['root']['launch_nonce']='0'*64
  elif label=='runtime':document['root']['runtime']='wsl'
  elif label=='gate_profile':document['gate']['profile_sha256']='0'*64
  elif label=='gate_birth':document['gate']['process']={}
  elif label=='stop_nonce':stop['launch_nonce']='0'*64
  else:values['tail_text']='x'*(records.TAIL_BYTES+1)
  values['process_json']=json.dumps(document);values['stop_json']=json.dumps(stop)
  value=records.public(SimpleNamespace(**values));assert value['phase']=='evidence_invalid' and value['process'] is None and value['cleanup_verified'] is None
  checked.append(label)
 return {'rejected':checked}

@app.on_event('startup')
async def watch():
 async def die():
  while not (root/'exit-owner').exists():await asyncio.sleep(.02)
  os._exit(124)
 asyncio.create_task(die())
'''

LEAF='''import importlib.util,json,os,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('terminal_child_identity',sys.argv[1]);identity=importlib.util.module_from_spec(spec);spec.loader.exec_module(identity)
marker=Path(sys.argv[2]);temporary=marker.with_suffix('.writing');temporary.write_text(json.dumps(identity.observe(os.getpid())));temporary.replace(marker)
heartbeat=Path(sys.argv[3])
while True:
 with heartbeat.open('ab') as stream:stream.write(b'x')
 time.sleep(.02)
'''
SPAWN='''import os,subprocess,sys
argv=[getattr(sys,'_base_executable',sys.executable),'-I','-S','-u',*sys.argv[1:]]
subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
 start_new_session=os.name!='nt',creationflags=(subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0)
'''
BULK='''import hashlib,json,os,sys,time
from pathlib import Path
size=int(sys.argv[1]);ready=Path(sys.argv[2]);done=Path(sys.argv[3])
if os.name=='nt':
 import ctypes,msvcrt
 from ctypes import wintypes
 kernel=ctypes.WinDLL('kernel32',use_last_error=True)
 kernel.GetConsoleMode.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)];kernel.GetConsoleMode.restype=wintypes.BOOL
 kernel.SetConsoleMode.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.SetConsoleMode.restype=wintypes.BOOL
 handle=msvcrt.get_osfhandle(0);mode=wintypes.DWORD()
 if not kernel.GetConsoleMode(handle,ctypes.byref(mode)):raise ctypes.WinError(ctypes.get_last_error())
 if not kernel.SetConsoleMode(handle,mode.value&~7):raise ctypes.WinError(ctypes.get_last_error())
 msvcrt.setmode(0,os.O_BINARY)
else:
 import termios
 mode=termios.tcgetattr(0);raw=termios.tcgetattr(0);raw[3]&=~(termios.ICANON|termios.ECHO);raw[6][termios.VMIN]=1;raw[6][termios.VTIME]=0
 termios.tcsetattr(0,termios.TCSANOW,raw)
try:
 ready.write_text('ready')
 if len(sys.argv)>4 and sys.argv[4]=='hold':
  while True:time.sleep(.1)
 received=0;digest=hashlib.sha256()
 while received<size:
  data=os.read(0,min(16384,size-received))
  if not data:raise EOFError('Terminal input ended early')
  received+=len(data);digest.update(data)
 temporary=done.with_suffix('.writing');temporary.write_text(json.dumps({'bytes':received,'sha256':digest.hexdigest()}));temporary.replace(done)
finally:
 if os.name=='nt':kernel.SetConsoleMode(handle,mode.value)
 else:termios.tcsetattr(0,termios.TCSANOW,mode)
print('BULK_INPUT_FINISHED',flush=True)
'''


def state(api):return api.call('GET','/fixture/state',api_prefix=False)


async def poll(api,condition,seconds=25):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=state(api)
        if condition(value):return value
        await asyncio.sleep(.04)
    raise TimeoutError(json.dumps(value))


async def connect(client,api,shell='cmd',*,case=None,identifier=None):
    params={'token':api.token}
    if identifier is not None:params['session']=identifier
    else:params.update(shell=shell,case=api.case_id if case is None else case)
    ws=await client.ws_connect(api.base+'/ws/terminal',params=params)
    return ws


async def ready(ws):
    frame=await asyncio.wait_for(ws.receive(),35)
    assert frame.type==aiohttp.WSMsgType.TEXT,frame
    value=json.loads(frame.data);assert value['type']=='ready',value
    return value['session']


async def command_marker(api,ws,shell,filename):
    text=('echo PTY_ACTUAL_OK>'+filename+'\r\n') if shell=='cmd' else ("printf 'PTY_ACTUAL''_OK' > "+filename+'; cat '+filename+'; stty size\n')
    await ws.send_json({'type':'resize','cols':95,'rows':35});await ws.send_json({'type':'input','data':text})
    if shell=='wsl-sandbox':
        output=''
        while 'PTY_ACTUAL_OK' not in output or '35 95' not in output:
            message=await asyncio.wait_for(ws.receive(),15)
            assert message.type==aiohttp.WSMsgType.BINARY,message
            output+=message.data.decode('utf8','replace')
    else:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            files=list((api.root/'files').rglob(filename))
            if files and files[0].read_text().strip()=='PTY_ACTUAL_OK':break
            await asyncio.sleep(.03)
        else:raise AssertionError('The terminal did not execute its command')


async def exited(row,gate=None):
    root=row['process']['root'] if row['process'] else None
    if root is not None:
        assert identity.probe(root['host'])['state']=='exited'
        if root['native'] is not None:assert (await probe_native(root['native']['distribution'],root['native']['process']))['state']=='exited'
    actual=gate or (row['process']['gate'] if row['process'] else None)
    if actual is not None:
        if actual['process']['platform']=='windows':assert identity.probe(actual['process'])['state']=='exited'
        else:assert (await probe_native(root['native']['distribution'],actual['process']))['state']=='exited'


async def detached(api,ws,shell,identifier):
    marker=api.root/('detached-'+identifier+'.json');heartbeat=marker.with_suffix('.heartbeat')
    argv=[str(api.root/'terminal_leaf.py'),str(SERVER/'app/services/process_identity_native.py'),str(marker),str(heartbeat)]
    if shell=='cmd':command=subprocess.list2cmdline([sys._base_executable,'-I','-S',str(api.root/'terminal_spawn.py'),*argv])+'\r\n'
    else:command=shlex.join(['python3','-I','-S',wsl_path(api.root/'terminal_spawn.py'),*(wsl_path(value) for value in argv)])+'\n'
    await ws.send_json({'type':'input','data':command})
    deadline=time.monotonic()+10
    while not marker.exists() or not heartbeat.exists():
        if time.monotonic()>=deadline:raise TimeoutError('Detached terminal child did not start')
        await asyncio.sleep(.03)
    reported=json.loads(marker.read_text())
    if shell=='cmd':
        assert identity.probe(reported)['state']=='match';return {'runtime':'windows','birth':reported,'heartbeat':str(heartbeat)}
    row=api.call('GET','/terminal/sessions/'+identifier);root=row['process']['root']['native']['process']
    result=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S',wsl_path(PROBE)],
        input=json.dumps({'root':root,'reported':reported}).encode(),capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr.decode('utf8','replace')
    birth=json.loads(result.stdout);assert (await probe_native('vibeflow-linux',birth))['state']=='match'
    return {'runtime':'wsl','birth':birth,'heartbeat':str(heartbeat)}


async def detached_exited(member):
    value=identity.probe(member['birth']) if member['runtime']=='windows' else await probe_native('vibeflow-linux',member['birth'])
    assert value['state']=='exited',value


async def bulk_input(api,client,shell,*,unread=False):
    ws=await connect(client,api,shell);identifier=await ready(ws)
    start=api.root/('bulk-'+identifier+'.ready');done=start.with_suffix('.json');events=[]
    async def drain():
        async for frame in ws:
            if frame.type==aiohttp.WSMsgType.TEXT:events.append(json.loads(frame.data))
    consumer=asyncio.create_task(drain())
    size=2*1024*1024
    argv=[str(size),str(start),str(done)]
    if unread:argv.append('hold')
    command=(subprocess.list2cmdline([sys._base_executable,'-I','-S',str(api.root/'terminal_bulk.py'),*argv])+'\r\n' if shell=='cmd' else
             shlex.join(['python3','-I','-S',wsl_path(api.root/'terminal_bulk.py'),str(size),wsl_path(start),wsl_path(done),*(['hold'] if unread else [])])+'\n')
    try:
        await ws.send_json({'type':'input','data':command})
        deadline=time.monotonic()+15
        while True:
            listed=next(item for item in api.call('GET','/terminal/sessions')['sessions'] if item['id']==identifier)
            if start.exists() and listed['pending_input_bytes']==0:break
            if time.monotonic()>=deadline:raise TimeoutError('Raw terminal reader did not become ready')
            await asyncio.sleep(.03)
        await ws.send_json({'type':'input','data':'x'*(size+1)})
        deadline=time.monotonic()+10
        while not any(event.get('code')=='terminal_input_limit' for event in events):
            if time.monotonic()>=deadline:raise TimeoutError('Oversized input was not rejected')
            await asyncio.sleep(.03)
        assert api.call('GET','/terminal/sessions/'+identifier)['phase']=='active'
        await ws.send_json({'type':'input','data':'x'*size})
        # Resize storms coalesce while delivery credits prevent input from
        # filling the separate stop-control path.
        for width in range(80,120):await ws.send_json({'type':'resize','cols':width,'rows':30})
        if unread:
            assert not done.exists()
            began=time.monotonic();api.put('E-report/terminal-unread-concurrent.txt',identifier)
            assert time.monotonic()-began<2
            await ws.close();await consumer
            assert api.call('GET','/terminal/sessions/'+identifier)['phase']=='active'
            began=time.monotonic();api.call('DELETE','/terminal/sessions/'+identifier)
            assert time.monotonic()-began<10
            row=api.call('GET','/terminal/sessions/'+identifier);assert row['phase']=='closed' and row['cleanup_verified'] is True
            await exited(row);return row
        deadline=time.monotonic()+45
        while not done.exists():
            if time.monotonic()>=deadline:raise TimeoutError('Full terminal input did not reach the actual reader')
            assert api.call('GET','/terminal/sessions/'+identifier)['phase']=='active'
            await asyncio.sleep(.05)
        assert json.loads(done.read_text())=={'bytes':size,'sha256':hashlib.sha256(b'x'*size).hexdigest()}
        assert not any(event.get('type')=='exit' for event in events)
        api.call('DELETE','/terminal/sessions/'+identifier)
        row=api.call('GET','/terminal/sessions/'+identifier);assert row['cleanup_verified'] is True
        await exited(row)
        return row
    finally:
        await ws.close();await consumer


async def verify(database_url=None):
    server=IsolatedServer('terminal-execution',app='controlled_terminal:app',database_url=database_url)
    (server.root/'controlled_terminal.py').write_text(CONTROLLED,encoding='utf8')
    (server.root/'terminal_leaf.py').write_text(LEAF,encoding='utf8')
    (server.root/'terminal_spawn.py').write_text(SPAWN,encoding='utf8')
    (server.root/'terminal_bulk.py').write_text(BULK,encoding='utf8')
    sentinel_code="import sys,json;sys.path.insert(0,sys.argv[1]);import process_identity_native as i;print(json.dumps(i.observe(__import__('os').getpid())),flush=True);sys.stdin.buffer.read(1)"
    services=SERVER/'app/services'
    host=subprocess.Popen([sys._base_executable,'-I','-S','-u','-c',sentinel_code,str(services)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    native=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',sentinel_code,wsl_path(services)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    foreign={'windows':json.loads(host.stdout.readline()),'linux':json.loads(native.stdout.readline())}
    checks=[];runs=[];readbacks=[];death=[]
    def passed(name,row=None):
        checks.append(name)
        if row is not None:runs.append({'mode':name,'session':row});readbacks.append(row)
        assert host.poll() is None and native.poll() is None
        print('PASS terminal_'+name,flush=True)
    try:
        with server as api:
            api.call('GET','/terminal/sessions',expect=401);api.create_case('Owned terminals');token=api.token
            assert state(api)['sessions']==[];passed('anonymous_no_allocation')
            async with aiohttp.ClientSession() as client:
                for shell in ('cmd','wsl','wsl-sandbox'):
                    ws=await connect(client,api,shell);identifier=await ready(ws)
                    row=api.call('GET','/terminal/sessions/'+identifier);assert row['phase']=='active' and row['process']['gate']
                    filename='actual-'+shell+'.txt';await command_marker(api,ws,shell,filename)
                    await ws.close();assert api.call('GET','/terminal/sessions/'+identifier)['process']==row['process']
                    again=await connect(client,api,identifier=identifier);assert await ready(again)==identifier
                    assert api.call('GET','/terminal/sessions/'+identifier)['process']==row['process']
                    await again.send_json({'type':'input','data':'exit /b 7\r\n' if shell=='cmd' else 'exit 7\n'})
                    while True:
                        frame=await asyncio.wait_for(again.receive(),15)
                        if frame.type==aiohttp.WSMsgType.TEXT:
                            terminal=json.loads(frame.data);assert terminal['type']=='exit' and terminal['code']==7,terminal;break
                    await again.close();row=api.call('GET','/terminal/sessions/'+identifier)
                    assert row['phase']=='closed' and row['cleanup_verified'] is True and row['exit_code']==7
                    assert list((api.root/'files').rglob(filename))[0].read_text().strip()=='PTY_ACTUAL_OK'
                    await exited(row);api.call('DELETE','/terminal/sessions/'+identifier);api.call('DELETE','/terminal/sessions/'+identifier)
                    passed(shell+'_io_resize_reconnect_exit',row)
                global_ws=await connect(client,api,case=0);identifier=await ready(global_ws)
                row=api.call('GET','/terminal/sessions/'+identifier);assert row['case_id'] is None
                await global_ws.close();api.call('DELETE','/terminal/sessions/'+identifier)
                row=api.call('GET','/terminal/sessions/'+identifier);assert row['cleanup_verified'] and row['exit_code'] is None
                await exited(row);passed('global_user_session_without_case',row)
                for shell in ('cmd','wsl'):
                    row=await bulk_input(api,client,shell);passed(shell+'_2mib_input_bound_resize_coalescing',row)
                    row=await bulk_input(api,client,shell,unread=True);passed(shell+'_unread_input_disconnect_and_explicit_close',row)
                for shell in ('cmd','wsl'):
                    for mode in ('hold','cancel_startup'):
                        api.call('POST','/fixture/mode/'+mode,foreign,api_prefix=False)
                        before={row['id'] for row in state(api)['sessions']};ws=await connect(client,api,shell)
                        value=await poll(api,lambda value:any(row['id'] not in before and (api.root/('gated-'+row['id']+'.json')).exists() for row in value['sessions']))
                        row=next(row for row in value['sessions'] if row['id'] not in before);identifier=row['id']
                        gate=json.loads((api.root/('gated-'+identifier+'.json')).read_text())
                        assert row['phase']=='starting' and row['process']['root'] is not None and row['process']['gate'] is None
                        if shell=='cmd':
                            members=api.call('GET','/fixture/members/'+identifier,api_prefix=False)['members']
                            assert any(item['identity']==gate['process'] for item in members) and not any(item['name']=='cmd.exe' for item in members),members
                        else:
                            members=inspect_native({'root':row['process']['root']['native']['process']})
                            assert not any(item['comm']=='bash' for item in members),members
                        api.put('E-report/terminal-concurrent.txt',identifier)
                        assert api.call('GET','/api/health',api_prefix=False)['ok']
                        if mode=='hold':
                            (api.root/('release-'+identifier)).touch();assert await ready(ws)==identifier
                            api.call('DELETE','/terminal/sessions/'+identifier)
                        else:api.call('POST','/fixture/cancel-startup/'+identifier,api_prefix=False)
                        await ws.close();row=api.call('GET','/terminal/sessions/'+identifier)
                        assert row['phase']==('closed' if mode=='hold' else 'failed') and row['cleanup_verified'] is True
                        await exited(row,gate);passed(shell+'_'+mode,row)
                    for mode in ('bad_root','root_commit','bad_gate','foreign_gate','gate_profile','gate_commit','release_failure','expired_lease'):
                        api.call('POST','/fixture/mode/'+mode,foreign,api_prefix=False)
                        before={row['id'] for row in state(api)['sessions']};ws=await connect(client,api,shell)
                        frame=await asyncio.wait_for(ws.receive(),35);assert frame.type==aiohttp.WSMsgType.TEXT,frame
                        assert json.loads(frame.data).get('type')=='error',frame.data;await ws.close()
                        value=await poll(api,lambda value:any(row['id'] not in before and row['phase']=='failed' for row in value['sessions']))
                        row=next(row for row in value['sessions'] if row['id'] not in before)
                        assert row['cleanup_verified'] is True and row['error'] is not None,row
                        await exited(row);passed(shell+'_'+mode,row)
                    for mode in ('idle','heartbeat_failure','unverified_stop','receipt_failure'):
                        api.call('POST','/fixture/mode/'+mode,foreign,api_prefix=False)
                        ws=await connect(client,api,shell);identifier=await ready(ws)
                        if mode=='idle':await ws.close()
                        elif mode=='heartbeat_failure':
                            assert api.call('POST','/fixture/heartbeat-failure/'+identifier,api_prefix=False)['armed']
                        elif mode in {'unverified_stop','receipt_failure'}:api.call('DELETE','/terminal/sessions/'+identifier,expect=502)
                        if mode=='receipt_failure':
                            row=api.call('GET','/terminal/sessions/'+identifier)
                            assert row['phase']=='active' and row['ended_at'] is None and row['stop'] is None
                        else:
                            await poll(api,lambda value:any(row['id']==identifier and row['phase']==('closed' if mode=='idle' else 'failed') for row in value['sessions']))
                            row=api.call('GET','/terminal/sessions/'+identifier)
                            assert row['cleanup_verified'] is (False if mode=='unverified_stop' else True)
                        if mode in {'unverified_stop','receipt_failure','heartbeat_failure'}:
                            while True:
                                frame=await asyncio.wait_for(ws.receive(),15)
                                if frame.type==aiohttp.WSMsgType.TEXT:
                                    assert json.loads(frame.data).get('type')=='error',frame.data;break
                        await ws.close();await exited(row);passed(shell+'_'+mode,row)
                api.call('POST','/fixture/mode/normal',api_prefix=False)
                owned=readbacks[0]
                assert len(api.call('GET','/fixture/journal/'+owned['id'],api_prefix=False)['rejected'])==6;passed('damaged_journal_rejected')
                other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'terminal-test-123','name':'Other'})['token']
                api.token=other;api.call('GET','/terminal/sessions/'+owned['id'],expect=404);api.call('DELETE','/terminal/sessions/'+owned['id'],expect=404)
                assert not api.call('GET','/terminal/sessions?include_closed=true')['sessions'];api.token=token;passed('user_scope_private')
                workspace=api.call('GET','/workspaces')[0]
                cases=[api.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'terminal cancel '+str(index)})['id'] for index in range(2)]
                first=await connect(client,api,case=cases[0]);first_id=await ready(first)
                second=await connect(client,api,case=cases[1]);second_id=await ready(second)
                result=api.call('POST','/runtime/processes:cancel',{'case_id':cases[0]});assert result['ok'] and result['cancelled']['terminal']==1,result
                row=api.call('GET','/terminal/sessions/'+first_id);assert row['phase']=='closed' and row['cleanup_verified']
                await exited(row);assert api.call('GET','/terminal/sessions/'+second_id)['phase']=='active'
                api.call('DELETE','/terminal/sessions/'+second_id);await first.close();await second.close();passed('case_cancel_waits_for_owned_terminal_only',row)
                for shell in ('cmd','wsl'):
                    ws=await connect(client,api,shell);identifier=await ready(ws)
                    member=await detached(api,ws,shell,identifier)
                    api.call('DELETE','/terminal/sessions/'+identifier);await ws.close()
                    row=api.call('GET','/terminal/sessions/'+identifier);assert row['cleanup_verified'] is True
                    await exited(row);await detached_exited(member);passed(shell+'_detached_child_close',row)
                    runs[-1]['detached_child']=member
                assert state(api)['jobs']==0
                # Kill the actual API while both native families have active
                # interactive roots; durable evidence must not be fabricated.
                api.call('POST','/fixture/mode/owner_death',api_prefix=False)
                connections=[];death_members=[]
                for shell in ('cmd','wsl'):
                    ws=await connect(client,api,shell);identifier=await ready(ws);connections.append(ws)
                    death_members.append(await detached(api,ws,shell,identifier))
                    row=api.call('GET','/terminal/sessions/'+identifier);death.append(row)
                (api.root/'exit-owner').touch();await asyncio.to_thread(api.proc.wait,15)
                for ws in connections:await ws.close()
                deadline=time.monotonic()+15
                for row,member in zip(death,death_members):
                    while True:
                        try:await exited(row);await detached_exited(member);break
                        except AssertionError:
                            if time.monotonic()>=deadline:raise
                            await asyncio.sleep(.05)
                passed('actual_owner_death_retires_both_families')
                runs.append({'mode':'owner_death','sessions':death,'detached_children':death_members})
        reader=IsolatedServer('terminal-reader',app='terminal_reader:app',database_url=database_url)
        overrides={key:server.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=server.env['VIBEFLOW_DATABASE_URL']
        (reader.root/'terminal_reader.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+CONTROLLED,encoding='utf8')
        with reader as api:
            api.token=token
            for row in readbacks:
                value=api.call('GET','/terminal/sessions/'+row['id']);assert value['process']==row['process'] and value['stop']==row['stop'] and value['ended_at']==row['ended_at']
            async with aiohttp.ClientSession() as client:
                before=len(state(api)['sessions']);ws=await connect(client,api,identifier=readbacks[0]['id'])
                assert await ready(ws)==readbacks[0]['id'];await ws.close();assert len(state(api)['sessions'])==before
                for row in death:
                    api.call('POST','/fixture/expire/'+row['id'],api_prefix=False);value=api.call('GET','/terminal/sessions/'+row['id'])
                    assert value['phase']=='recovery_required' and value['process']==row['process'] and value['ended_at'] is None and value['stop'] is None
                    ws=await connect(client,api,identifier=row['id']);frame=await asyncio.wait_for(ws.receive(),10)
                    assert json.loads(frame.data)['code']=='recovery_required';await ws.close()
                assert len(state(api)['sessions'])==before
            passed('restart_reads_tail_and_never_replays_incomplete_session')
            assert identity.probe(foreign['windows'])['state']=='match'
            assert (await probe_native('vibeflow-linux',foreign['linux']))['state']=='match'
    finally:
        for process in (host,native):process.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),'reader_evidence':str(reader.root),
        'jobs_not_allocated':True,'owner_death_no_fabricated_receipt':True,'gated_payload_before_commit':True,'detached_children_observed':True}
    (server.root/'terminal-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='terminal-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await verify()}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_terminal_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_terminal_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await verify(temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('TERMINAL_EXECUTION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
