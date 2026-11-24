"""User-owned capability probes: real Python/WSL and disposable fault evidence."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
from mpi_scope_smoke import inspect_native

CONTROLLED='''import asyncio,contextvars,json,os,sys
from pathlib import Path
from datetime import timedelta
from fastapi import Depends,HTTPException
from sqlalchemy import select,update,func
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import Case,Job,NativeRequest,User
from app.routers import runtime as router
from app.services import native_request_records as records,native_request_execution as execution,native_execution as native,runtime_probe_execution as runtime
from app.services.durable_events import now
from app.services.owned_process import wsl_path
root=Path(__file__).parent
mode='normal';settings={};channels={};unverified=set()
commit_failure=contextvars.ContextVar('probe_commit_failure',default=False)
original_commit=AsyncSession.commit
async def commit(db):
    if commit_failure.get():raise OSError('isolated probe commit failure')
    return await original_commit(db)
AsyncSession.commit=commit
create=records.create
async def creating(*args,**kwargs):
    owner=await create(*args,**kwargs);settings[owner.id]=mode
    return owner
records.create=creating
start=execution.RequestExecutor.start
async def starting(self):
    value=await start(self)
    channels[self.owner.id]={'address':self.address,'capability':self.local_capability}
    (root/('channel-'+self.owner.id+'.json')).write_text(json.dumps(channels[self.owner.id]))
    return value
execution.RequestExecutor.start=starting
register=records.register
async def registering(owner,nonce,identifier,birth,distribution):
    selected=settings[owner.id]
    target=(selected.startswith('python_') and birth['runtime']=='windows') or (not selected.startswith('python_') and birth['runtime']=='wsl')
    if target and selected in {'bad_birth','python_bad_birth'}:
        birth=json.loads(json.dumps(birth))
        if birth['native'] is None:birth['host']['creation_time_100ns']=str(int(birth['host']['creation_time_100ns'])+1)
        else:birth['native']['process']['start_ticks']=str(int(birth['native']['process']['start_ticks'])+1)
    if target and selected=='wrong_distro':distribution='openfoam2406-default'
    token=commit_failure.set(target and selected=='register_failure')
    try:return await register(owner,'0'*64 if target and selected=='bad_nonce' else nonce,identifier,birth,distribution)
    finally:commit_failure.reset(token)
records.register=registering
finish=records.finish
async def finishing(owner,*args):
    token=commit_failure.set(settings[owner.id]=='receipt_failure')
    try:return await finish(owner,*args)
    finally:commit_failure.reset(token)
records.finish=finishing
complete=records.complete
async def completing(owner,*args,**kwargs):
    token=commit_failure.set(settings[owner.id]=='complete_failure')
    try:return await complete(owner,*args,**kwargs)
    finally:commit_failure.reset(token)
records.complete=completing
heartbeat=records.heartbeat
async def beating(owner):
    if settings[owner.id]=='heartbeat_failure':raise OSError('isolated heartbeat failure')
    return await heartbeat(owner)
records.heartbeat=beating
command=execution.RequestExecutor._command
def commanding(self,purpose,parameters):
    selected=settings[self.owner.id]
    if selected=='profile_denied':parameters={**parameters,'unexpected':True}
    argv,deadline,limit=command(self,purpose,parameters)
    registration={'bad_birth','python_bad_birth','bad_nonce','wrong_distro','register_failure'}
    held={'hold','disconnect','owner_death','timeout','python_timeout','heartbeat_failure','log_limit'}|registration
    if selected in held or selected=='invalid_frame':
        target=selected in {'hold','disconnect','owner_death','invalid_frame','heartbeat_failure'} or (self.runtime=='windows' if selected.startswith('python_') else self.runtime=='wsl')
        if target:
            helper=root/'held_runtime.py'
            argv=([self.profile['python'],'-I','-u',str(helper)] if self.runtime=='windows' else
                  ['wsl.exe','-d',self.profile['distro'],'--exec','python3','-I','-S','-u',wsl_path(helper)])
            argv += [str(root) if self.runtime=='windows' else wsl_path(root),self.owner.id,selected,
                     str(runtime.NATIVE) if self.runtime=='windows' else wsl_path(runtime.NATIVE),
                     'python' if self.runtime=='windows' else 'wsl',json.dumps(self.profile)]
            if selected in {'timeout','python_timeout'}|registration:deadline=2
            if selected=='log_limit':limit=256
    return argv,deadline,limit
execution.RequestExecutor._command=commanding
stop=native.OwnedProcess._stop
async def stopping(self,*args):
    value=await stop(self,*args)
    if self.nonce in unverified:value['verified']=False;value['error']='OSError'
    return value
native.OwnedProcess._stop=stopping
execute=native.NativeExecutor._execute
async def executing(self,entry,*args):
    if settings.get(self.owner.id)=='unverified_stop':unverified.add(entry.owned.nonce)
    return await execute(self,entry,*args)
native.NativeExecutor._execute=executing
collect=router._collect_capabilities
async def collecting(user_id):
    value=await collect(user_id)
    if mode=='revoked':
        async with SessionLocal() as db:
            await db.execute(update(User).where(User.id==user_id).values(status='blocked'));await db.commit()
    return value
router._collect_capabilities=collecting

@app.post('/fixture/mode/{selected}')
async def select_mode(selected:str,user=Depends(get_current_user)):
    global mode
    mode=selected
    return {'ok':True}

@app.get('/fixture/state')
async def state(user=Depends(get_current_user),db=Depends(get_db)):
    rows=(await db.scalars(select(NativeRequest).where(NativeRequest.created_by==user.id).order_by(NativeRequest.created_at,NativeRequest.id))).all()
    return {'requests':[records.public(row) for row in rows],
      'case_count':await db.scalar(select(func.count()).select_from(Case)),
      'job_count':await db.scalar(select(func.count()).select_from(Job)),
      'loaded':{name:name in sys.modules for name in ('vtk','cadquery')}}

@app.get('/fixture/request/{identifier}')
async def request_row(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
    row=await db.get(NativeRequest,identifier)
    if row is None or row.created_by!=user.id:raise HTTPException(404)
    return records.public(row)

@app.post('/fixture/expire/{identifier}')
async def expire(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
    row=await db.get(NativeRequest,identifier)
    if row.created_by!=user.id:raise HTTPException(404)
    await db.rollback()
    async with SessionLocal() as session:
        await session.execute(update(NativeRequest).where(NativeRequest.id==identifier).values(lease_expires_at=now()-timedelta(seconds=1)))
        await session.commit()
    return {'ok':True}

@app.get('/fixture/scope/{identifier}')
async def scope_check(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
    from types import SimpleNamespace
    row=await db.get(NativeRequest,identifier)
    if row.created_by!=user.id:raise HTTPException(404)
    original={column.name:getattr(row,column.name) for column in NativeRequest.__table__.columns}
    invalid={**original,'case_id':1}
    assert records.public(SimpleNamespace(**invalid))['phase']=='evidence_invalid'
    invalid={**original,'kind':'report.prepare','case_id':None,'children_json':'{}'}
    assert records.public(SimpleNamespace(**invalid))['phase']=='evidence_invalid'
    return {'rejected':2}

@app.on_event('startup')
async def watch():
    async def die():
        while not (root/'exit-owner').exists():await asyncio.sleep(.02)
        os._exit(126)
    asyncio.create_task(die())
'''

HELD='''import base64,importlib.util,json,os,shlex,sys,time
from pathlib import Path
root=Path(sys.argv[1]);identifier=sys.argv[2];mode=sys.argv[3];helper=sys.argv[4];kind=sys.argv[5];profile=json.loads(sys.argv[6])
spec=importlib.util.spec_from_file_location('held_runtime_native',helper);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
marker=root/('payload-'+identifier+'.json');release=root/('release-'+identifier)
def mark(pid):
 temporary=marker.with_suffix('.writing');temporary.write_text(json.dumps({'local_pid':pid,'kind':kind}));temporary.replace(marker)
if mode=='log_limit':
 mark(os.getpid());print('x'*8192,flush=True)
 while True:time.sleep(.05)
elif mode!='invalid_frame':
 if kind=='python':
  original=module.importlib.import_module
  def importing(name,*args,**kwargs):
   if not marker.exists():mark(os.getpid())
   while not release.exists():time.sleep(.02)
   return original(name,*args,**kwargs)
  module.importlib.import_module=importing
 else:
  module.SCRIPT='while [ ! -e '+shlex.quote(str(release))+' ]; do sleep .02; done\\n'+module.SCRIPT
  original=module.subprocess.Popen
  def starting(*args,**kwargs):
   process=original(*args,**kwargs);mark(process.pid);return process
  module.subprocess.Popen=starting
settings={'kind':'python','packages':profile['packages']} if kind=='python' else {'kind':'wsl','bashrc':profile['bashrc']}
module.execute(base64.b64encode(json.dumps(settings).encode()).decode())
if mode=='invalid_frame':print(module.FRAME+'{}',flush=True)
'''


def state(api):return api.call('GET','/fixture/state',api_prefix=False)


def poll(api,identifiers,condition,seconds=35):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        rows=[row for row in state(api)['requests'] if row['id'] in identifiers]
        if len(rows)==len(identifiers) and condition(rows):return rows
        time.sleep(.04)
    raise TimeoutError(json.dumps(rows))


def request(api):
    stream=socket.create_connection(('127.0.0.1',api.port),5);stream.settimeout(45)
    stream.sendall(('GET /api/v1/runtime/capabilities HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer '+api.token+'\r\nConnection: close\r\n\r\n').encode())
    return stream


def received(stream):
    chunks=[]
    while True:
        chunk=stream.recv(65536)
        if not chunk:break
        chunks.append(chunk)
    stream.close();raw=b''.join(chunks)
    assert raw.startswith(b'HTTP/1.1 200 '),raw[:500]
    return json.loads(raw.split(b'\r\n\r\n',1)[1])


def exited(rows):
    for row in rows:
        if row['children'] is None:continue
        for child in row['children']['children'].values():
            birth=child['identity']
            if birth is None:continue
            assert identity.probe(birth['host'])['state']=='exited'
            if birth['native'] is not None:
                native=birth['native'];assert asyncio.run(probe_native(native['distribution'],native['process']))['state']=='exited'


def held_members(api,rows):
    members=[]
    for row in rows:
        marker=api.root/('payload-'+row['id']+'.json')
        deadline=time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists(),row
        child=next(iter(row['children']['children'].values()))
        if row['kind']=='runtime.python_probe':
            member=identity.observe(json.loads(marker.read_text())['local_pid'])
            assert identity.probe(member)['state']=='match'
            members.append({'runtime':'windows','birth':member})
        else:
            found=inspect_native({'root':child['identity']['native']['process']})
            tools=[item for item in found if item['comm']=='bash']
            assert len(tools)==1,found
            members.append({'runtime':'wsl','birth':tools[0]['identity']})
    return members


def members_exited(members):
    for member in members:
        value=(identity.probe(member['birth']) if member['runtime']=='windows' else
               inspect_native({'probe':[member['birth']]})[0])
        assert value['state']=='exited',value


def verify(database_url=None):
    server=IsolatedServer('runtime-probe',app='controlled_runtime:app',database_url=database_url)
    (server.root/'controlled_runtime.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'held_runtime.py').write_text(HELD,encoding='utf-8')
    checks=[];runs=[];persisted=[];death=None
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-c','import sys;sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    native_sentinel=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c','import sys;sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with server as api:
            api.call('GET','/runtime/capabilities',expect=401)
            api.token=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'runtime-test-123','name':'Runtime'})['token']
            token=api.token
            initial=state(api);assert not initial['requests'];checks.append('anonymous_no_allocation')
            modes=('normal','bad_birth','python_bad_birth','bad_nonce','wrong_distro','register_failure','profile_denied',
                   'timeout','python_timeout','log_limit','invalid_frame','unverified_stop','receipt_failure','complete_failure','heartbeat_failure','hold','disconnect')
            for mode in modes:
                before={row['id'] for row in state(api)['requests']}
                api.call('POST','/fixture/mode/'+mode,api_prefix=False)
                stream=request(api)
                members=[]
                if mode in {'hold','disconnect'}:
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        rows=[row for row in state(api)['requests'] if row['id'] not in before]
                        if len(rows)==2 and all(row['children'] and row['children']['children'] and all(c['phase']=='running' for c in row['children']['children'].values()) for row in rows):break
                        time.sleep(.04)
                    assert len(rows)==2
                    members=held_members(api,rows)
                    started=time.monotonic();assert api.call('GET','/api/health',api_prefix=False)['ok'];assert time.monotonic()-started<2
                    channel=json.loads((api.root/('channel-'+rows[0]['id']+'.json')).read_text())
                    private=socket.create_connection(('127.0.0.1',int(channel['address'].rsplit(':',1)[1])),5)
                    private.sendall((json.dumps({'schema':'vibeflow.native-call.v1','capability':'0'*64,'purpose':'python_runtime_probe','parameters':{}})+'\n').encode());private.settimeout(5)
                    answer=private.recv(16384);private.close();assert b'error' in answer
                    assert all(len(row['children']['children'])==1 for row in poll(api,[row['id'] for row in rows],lambda _:True))
                    if mode=='disconnect':stream.close();data=None
                    else:
                        for row in rows:(api.root/('release-'+row['id'])).touch()
                        data=received(stream)
                else:data=received(stream)
                current=state(api);rows=[row for row in current['requests'] if row['id'] not in before]
                rows=poll(api,[row['id'] for row in rows],lambda rows:all(row['phase']!='active' for row in rows)) if mode not in {'complete_failure','unverified_stop'} else rows
                assert len(rows)==2 and all(row['case_id'] is None for row in rows),rows
                assert current['case_count']==current['job_count']==0 and current['loaded']==initial['loaded']
                if mode in {'normal','hold'}:
                    assert data['python']['reachable'] and data['python']['packages']['cadquery'] and data['wsl']['openfoam'] and data['wsl']['wmake'],data
                    assert all(row['phase']=='succeeded' and row['cleanup_verified'] for row in rows)
                elif mode=='disconnect':assert all(row['phase']=='cancelled' and row['cleanup_verified'] for row in rows),rows
                else:
                    target='python' if mode.startswith('python_') else 'wsl'
                    if mode in {'profile_denied','invalid_frame','receipt_failure','complete_failure','unverified_stop','heartbeat_failure'}:assert not data['python']['reachable'] and not data['wsl']['reachable'],data
                    else:assert not data[target]['reachable'] and data['wsl' if target=='python' else 'python']['reachable'],data
                    if mode=='complete_failure':assert all(row['phase']=='active' for row in rows)
                    elif mode=='unverified_stop':
                        assert all(row['phase']=='active' and row['ended_at'] is None and row['cleanup_verified'] is False for row in rows),rows
                        assert all(child['phase']=='failed' and child['stop']['verified'] is False for row in rows for child in row['children']['children'].values())
                    elif mode=='receipt_failure':assert all(row['phase']=='failed' and not row['cleanup_verified'] for row in rows)
                    elif mode in {'profile_denied','invalid_frame','heartbeat_failure'}:assert all(row['phase']=='failed' for row in rows)
                    else:
                        for row in rows:assert row['phase']==('failed' if row['kind']=='runtime.'+target+'_probe' else 'succeeded'),row
                for row in rows:
                    if mode in {'bad_birth','python_bad_birth','bad_nonce','wrong_distro','register_failure'}:
                        assert not (api.root/('payload-'+row['id']+'.json')).exists()
                    if mode=='profile_denied':assert not row['children']['children']
                    if mode=='normal':
                        assert api.call('GET','/fixture/scope/'+row['id'],api_prefix=False)['rejected']==2
                    assert json.loads((api.root/('channel-'+row['id']+'.json')).read_text())['capability'] not in json.dumps(row)
                exited(rows);members_exited(members)
                assert sentinel.poll() is None and native_sentinel.poll() is None
                persisted.extend(rows);checks.append(mode);runs.append({'mode':mode,'requests':rows,'members':members})
                print('PASS runtime_probe_'+mode,flush=True)
            api.call('POST','/fixture/mode/normal',api_prefix=False)
            configured=api.call('GET','/runtime/config')
            inert=api.root/'configured-pandoc.cmd';inert.write_text('exit /b 125\n')
            api.call('PATCH','/runtime/config',{'python':sys._base_executable,'pandoc':str(inert)})
            selected=api.call('GET','/runtime/capabilities')
            assert selected['python']['reachable'] and selected['python']['packages']['cadquery'] is None
            assert selected['binaries']['pandoc']==str(inert)
            api.call('PATCH','/runtime/config',configured);checks.append('configured_tools_used')
            vanished=api.root/'vanished-python.exe';vanished.write_bytes(b'isolated missing configured executable')
            api.call('PATCH','/runtime/config',{'python':str(vanished)});vanished.unlink()
            selected=api.call('GET','/runtime/capabilities')
            assert not selected['python']['reachable'] and selected['python']['request_id'] is None and selected['wsl']['openfoam']
            assert all(value is None for value in selected['python']['packages'].values())
            api.call('PATCH','/runtime/config',configured);checks.append('vanished_configured_python_no_fallback')
            api.call('PATCH','/runtime/config',{'openfoam_bashrc':'/usr/lib/openfoam/not-installed/etc/bashrc'})
            selected=api.call('GET','/runtime/capabilities')
            assert selected['wsl']['reachable'] and not selected['wsl']['openfoam'] and not selected['wsl']['wmake']
            api.call('PATCH','/runtime/config',configured);checks.append('missing_openfoam_not_available')
            other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'runtime-test-123','name':'Other'})['token']
            api.token=other;api.call('GET','/fixture/request/'+persisted[0]['id'],expect=404,api_prefix=False);api.token=token
            checks.append('user_scope_private')
            before={row['id'] for row in state(api)['requests']};api.call('POST','/fixture/mode/owner_death',api_prefix=False);stream=request(api)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                rows=[row for row in state(api)['requests'] if row['id'] not in before]
                if len(rows)==2 and all(row['children'] and row['children']['children'] and all(c['phase']=='running' for c in row['children']['children'].values()) for row in rows):break
                time.sleep(.04)
            members=held_members(api,rows)
            rows=poll(api,[row['id'] for row in rows],lambda _:True);death=rows
            (api.root/'exit-owner').touch();api.proc.wait(timeout=15);stream.close()
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                try:exited(rows);members_exited(members);break
                except AssertionError:time.sleep(.05)
            exited(rows);members_exited(members);checks.append('owner_death');runs.append({'mode':'owner_death','requests':rows,'members':members})
        reader=IsolatedServer('runtime-probe-reader',app='runtime_reader:app',database_url=database_url)
        overrides={key:server.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=server.env['VIBEFLOW_DATABASE_URL']
        (reader.root/'runtime_reader.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+CONTROLLED,encoding='utf-8')
        with reader as api:
            api.token=token
            for row in persisted:
                value=api.call('GET','/fixture/request/'+row['id'],api_prefix=False)
                assert value['children']==row['children'] and value['ended_at']==row['ended_at']
            for row in death:
                api.call('POST','/fixture/expire/'+row['id'],api_prefix=False)
                value=api.call('GET','/fixture/request/'+row['id'],api_prefix=False)
                assert value['phase']=='recovery_required' and value['ended_at'] is None and value['children']==row['children']
                assert all(child['stop'] is None for child in value['children']['children'].values())
            checks.append('restart_reads_no_fabricated_receipt')
            api.call('POST','/fixture/mode/revoked',api_prefix=False)
            api.call('GET','/runtime/capabilities',expect=401);checks.append('revoked_before_result')
    finally:
        sentinel.communicate(b'x',timeout=10);native_sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),'reader_evidence':str(reader.root),
      'no_case_or_job_allocated':True,'heavy_imports_outside_api':True,'owner_death_no_fabricated_receipt':True}
    (server.root/'runtime-probe-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='runtime-probe-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_runtime_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_runtime_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('RUNTIME_PROBE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
