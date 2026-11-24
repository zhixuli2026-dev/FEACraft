"""Disposable request owners: real Pandoc/WSL, cancellation and durable failure evidence."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity

CONTROLLED='''import asyncio,contextvars,json,os,sys
from pathlib import Path
from sqlalchemy import select,update,func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from app.main import app
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import NativeRequest,Job
from app.routers.workspaces import get_owned_case
from app.services import native_request_execution as execution,native_request_records as records
from app.services import native_execution as native,process_identity_native as identity,pandoc_execution,latex_artifacts
from app.services.runtime_config import get_runtime_setting
root=Path(__file__).parent
tasks={};settings={};results={};unverified=set()
fail_commit=contextvars.ContextVar('request_commit_failure',default=False)
slow_probe=contextvars.ContextVar('request_slow_probe',default=False)
probe=records.os_identity.probe
def probing(birth):
    import time
    value=probe(birth)
    if slow_probe.get():
        (root/('late-probe-'+str(birth['pid']))).touch();time.sleep(1.3)
    return value
records.os_identity.probe=probing
commit=AsyncSession.commit
async def committing(db):
    if fail_commit.get():raise OSError('isolated request commit failure')
    return await commit(db)
AsyncSession.commit=committing
register=records.register
async def registering(owner,nonce,identifier,birth,distribution):
    mode=settings[owner.id]
    if mode=='bad_birth':
        birth=json.loads(json.dumps(birth));birth['host']['creation_time_100ns']=str(int(birth['host']['creation_time_100ns'])+1)
    if mode=='expired_lease':
        from datetime import timedelta
        from app.services.durable_events import now
        async with SessionLocal() as db:
            await db.execute(update(NativeRequest).where(NativeRequest.id==owner.id).values(lease_expires_at=now()+timedelta(seconds=1)))
            await db.commit()
    token=fail_commit.set(mode=='register_failure')
    probe_token=slow_probe.set(mode=='expired_lease')
    try:return await register(owner,'0'*64 if mode=='bad_nonce' else nonce,identifier,birth,distribution)
    finally:fail_commit.reset(token);slow_probe.reset(probe_token)
records.register=registering
finish=records.finish
async def finishing(owner,*args):
    token=fail_commit.set(settings[owner.id]=='receipt_failure')
    try:return await finish(owner,*args)
    finally:fail_commit.reset(token)
records.finish=finishing
heartbeat=records.heartbeat
async def beating(owner):
    if settings[owner.id]=='heartbeat_failure':raise OSError('isolated lease failure')
    return await heartbeat(owner)
records.heartbeat=beating
command=execution.RequestExecutor._command
def commands(self,purpose,parameters):
    argv,timeout,limit=command(self,purpose,parameters)
    mode=settings[self.owner.id]
    if self.kind=='report' and mode not in {'normal','profile_denied'}:
        argv=[sys._base_executable,'-I','-S','-u',str(root/'request_wait.py'),self.profile['--pandoc'],str(root/('tool-'+self.owner.id))]
        if mode in {'timeout','receipt_failure','unverified_stop'}:timeout=1
    return argv,timeout,limit
execution.RequestExecutor._command=commands
stop=native.OwnedProcess._stop
async def stopping(self,*args):
    result=await stop(self,*args)
    if self.nonce in unverified:result['verified']=False;result['error']='OSError'
    return result
native.OwnedProcess._stop=stopping
execute=native.NativeExecutor._execute
async def executing(self,entry,*args):
    if self.owner.id in settings and settings[self.owner.id]=='unverified_stop':unverified.add(entry.owned.nonce)
    return await execute(self,entry,*args)
native.NativeExecutor._execute=executing

@app.post('/fixture/start/{case_id}/{mode}')
async def start(case_id:int,mode:str,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db)
    prefix=case.s3_prefix;user_id=user.id
    from app.storage import _fs_path
    cwd=_fs_path(prefix,'.');cwd.mkdir(parents=True,exist_ok=True)
    await db.rollback()
    ready=asyncio.Event();holder={}
    async def work():
        try:
            kind='latex.prepare' if mode=='latex_probe' else 'report.prepare'
            profile=({'distro':get_runtime_setting('wsl_distro','vibeflow-linux'),'recipe':'pdflatex'} if mode=='latex_probe' else
                {'pandoc':str(Path(get_runtime_setting('pandoc',PANDOC)).resolve())})
            async with execution.scope(case_id,user_id,cwd,kind,profile) as executor:
                identifier=executor.owner.id;holder['id']=identifier;settings[identifier]=mode
                tasks[identifier]=asyncio.current_task()
                (root/('channel-'+identifier+'.json')).write_text(json.dumps({'address':executor.address,'capability':executor.local_capability}))
                ready.set()
                if mode=='latex_probe':value=await executor.run_sync(latex_artifacts.probe,profile['distro'],'pdflatex')
                elif mode=='profile_denied':
                    from app.services.native_execution_client import run_native
                    value=await executor.run_sync(run_native,'pandoc_version',{},timeout=5,capture=True)
                else:value=await executor.run_sync(pandoc_execution.run,'pandoc_parse',profile['pandoc'],cwd,markdown=b'# Owned request\\n')
                results[identifier]={'returncode':getattr(value,'returncode',0)}
        except BaseException as error:
            if 'id' in holder:results[holder['id']]={'error':type(error).__name__}
            else:holder['error']=type(error).__name__;ready.set()
    asyncio.create_task(work())
    await ready.wait()
    return holder

@app.get('/fixture/request/{identifier}')
async def read(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
    row=await db.get(NativeRequest,identifier,populate_existing=True)
    await get_owned_case(row.case_id,user,db,write=False)
    value=records.public(row);value['result']=results.get(identifier)
    return value

@app.post('/fixture/cancel/{identifier}')
async def cancel(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
    row=await db.get(NativeRequest,identifier)
    await get_owned_case(row.case_id,user,db)
    await db.rollback()
    tasks[identifier].cancel();await asyncio.gather(tasks[identifier],return_exceptions=True)
    return {'ok':True}

@app.post('/fixture/owner-check/{identifier}')
async def owner_check(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
    row=await db.get(NativeRequest,identifier)
    await get_owned_case(row.case_id,user,db)
    owner=records.Owner(identifier,'0'*64,row.owner_id,row.launch_nonce)
    await db.rollback()
    try:await records.heartbeat(owner)
    except Exception as error:return {'rejected':getattr(error,'code',type(error).__name__)}
    raise AssertionError('Incorrect request owner was accepted')

@app.get('/fixture/job-count/{case_id}')
async def count(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    return {'count':await db.scalar(select(func.count()).select_from(Job).where(Job.case_id==case_id))}

@app.get('/fixture/journal-check/{identifier}')
async def journal_check(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
    from types import SimpleNamespace
    row=await db.get(NativeRequest,identifier)
    await get_owned_case(row.case_id,user,db,write=False)
    original={column.name:getattr(row,column.name) for column in NativeRequest.__table__.columns}
    checked=[]
    for label in ('request_nonce','purpose','runtime','stop_nonce','owner_birth'):
        values=dict(original);journal=json.loads(values['children_json']);child=next(iter(journal['children'].values()))
        if label=='request_nonce':child['parent_launch_nonce']='0'*64
        elif label=='purpose':child['purpose']='latex_probe'
        elif label=='runtime':child['identity']['runtime']='wsl'
        elif label=='stop_nonce':child['stop']['launch_nonce']='0'*64
        else:values['owner_identity_json']='{}'
        values['children_json']=json.dumps(journal)
        projected=records.public(SimpleNamespace(**values))
        assert projected['phase']=='evidence_invalid' and projected['children'] is None and projected['cleanup_verified'] is None
        checked.append(label)
    return {'rejected':checked}

@app.on_event('startup')
async def owner_watch():
    async def watch():
        while not (root/'exit-request-owner').exists():await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''
WAIT='''import json,subprocess,sys
from pathlib import Path
marker=Path(sys.argv[2]);marker.with_suffix('.released').touch()
tool=subprocess.Popen([sys.argv[1],'--from=markdown','--to=json'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
tool.stdin.write(b'# Actual Pandoc request\\n');tool.stdin.flush()
temporary=marker.with_suffix('.writing');temporary.write_text(json.dumps({'pid':tool.pid}));temporary.replace(marker)
tool.wait()
'''


def wait(api,identifier,condition,seconds=45):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        row=api.call('GET','/fixture/request/'+identifier,api_prefix=False)
        if condition(row):return row
        time.sleep(.03)
    raise TimeoutError(json.dumps(row))


def connect(channel,purpose,parameters):
    import socket
    sock=socket.create_connection(('127.0.0.1',int(channel['address'].rsplit(':',1)[1])),5)
    sock.settimeout(10)
    request={'schema':'vibeflow.native-call.v1','capability':channel['capability'],'purpose':purpose,'parameters':parameters}
    sock.sendall((json.dumps(request)+'\n').encode())
    return sock


def verify(database_url=None):
    pandoc=str(SERVER.parents[1]/'extracted/share/vibeflow/tools/pandoc/pandoc.exe')
    server=IsolatedServer('native-request',app='request_controlled:app',database_url=database_url)
    (server.root/'request_controlled.py').write_text('PANDOC='+repr(pandoc)+'\n'+CONTROLLED,encoding='utf-8')
    (server.root/'request_wait.py').write_text(WAIT,encoding='utf-8')
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-c','import sys;sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    checks=[];readbacks=[];runs=[];death=None
    modes=('normal','latex_probe','profile_denied','cancel','timeout','disconnect','bad_birth','bad_nonce','expired_lease',
           'register_failure','receipt_failure','unverified_stop','heartbeat_failure','owner_death')
    try:
        with server as api:
            api.create_case('Request scopes');token=api.token
            for mode in modes:
                before=api.call('GET','/fixture/job-count/'+str(api.case_id),api_prefix=False)['count']
                started=api.call('POST',f'/fixture/start/{api.case_id}/{mode}',api_prefix=False)
                assert 'id' in started,started
                identifier=started['id'];members=[];child=None
                if mode not in {'normal','latex_probe','profile_denied','bad_birth','bad_nonce','expired_lease','register_failure'}:
                    row=wait(api,identifier,lambda row:any(c['phase']=='running' for c in row['children']['children'].values()))
                    child=next(iter(row['children']['children'].values()))
                    deadline=time.monotonic()+10
                    marker=api.root/('tool-'+identifier)
                    while not marker.exists() and time.monotonic()<deadline:time.sleep(.02)
                    assert marker.exists(),mode
                    members=[identity.observe(json.loads(marker.read_text())['pid'])]
                    if mode in {'cancel','disconnect','owner_death'}:
                        # A cooperating case write must finish while a real tool
                        # and its request scope are still active.
                        api.put('E-report/request-concurrent.txt','scope '+identifier)
                        denied=api.call('POST','/fixture/owner-check/'+identifier,api_prefix=False)
                        assert denied['rejected']=='state_conflict'
                    if mode=='cancel':api.call('POST','/fixture/cancel/'+identifier,api_prefix=False)
                    elif mode=='disconnect':
                        # Use the exact private fixed profile, then close its
                        # control transport while its real second tool waits.
                        channel=json.loads((api.root/('channel-'+identifier+'.json')).read_text())
                        # The original call already has a validated config; the
                        # fixture records it under this exclusive case parent.
                        configs=list(Path(api.env['VIBEFLOW_STORAGE_FS_ROOT']).rglob('settings.json'))
                        config=next(path for path in configs if path.parent.name.startswith('.pandoc-call-'))
                        sock=connect(channel,'pandoc_parse',{'config':str(config),'sha256':hashlib.sha256(config.read_bytes()).hexdigest()})
                        row=wait(api,identifier,lambda row:len(row['children']['children'])==2 and
                                 all(c['phase']=='running' for c in row['children']['children'].values()))
                        deadline=time.monotonic()+10
                        while json.loads(marker.read_text())['pid']==members[0]['pid'] and time.monotonic()<deadline:time.sleep(.02)
                        second=identity.observe(json.loads(marker.read_text())['pid'])
                        assert second['pid']!=members[0]['pid'];members.append(second)
                        sock.close()
                        row=wait(api,identifier,lambda row:any(c['phase']=='failed' for c in row['children']['children'].values()))
                        assert row['phase']=='active'
                        api.call('POST','/fixture/cancel/'+identifier,api_prefix=False)
                    elif mode=='owner_death':
                        death={'row':row,'members':members,'token':token};(api.root/'exit-request-owner').touch()
                        deadline=time.monotonic()+15
                        while identity.probe(row['owner_identity'])['state']!='exited' and time.monotonic()<deadline:time.sleep(.02)
                        assert identity.probe(row['owner_identity'])['state']=='exited'
                        for member in [child['identity']['host'],*members]:
                            while identity.probe(member)['state']!='exited' and time.monotonic()<deadline:time.sleep(.02)
                            assert identity.probe(member)['state']=='exited'
                        runs.append({'mode':mode,'request_id':identifier,'members':members});checks.append(mode)
                        break
                row=wait(api,identifier,lambda row:row['phase']!='active' and row['result'] is not None)
                assert row['phase']==('succeeded' if mode in {'normal','latex_probe'} else 'cancelled' if mode in {'cancel','disconnect'} else 'failed'),row
                children=row['children']['children']
                if mode=='profile_denied':assert not children
                elif mode=='receipt_failure':assert next(iter(children.values()))['phase']=='running' and row['cleanup_verified'] is False
                else:
                    assert children and all(c['stop'] is not None for c in children.values()),row
                    if mode=='unverified_stop':assert row['cleanup_verified'] is False
                    else:assert row['cleanup_verified'] is True,row
                    for item in children.values():
                        assert identity.probe(item['identity']['host'])['state']=='exited'
                        if item['identity']['native'] is not None:
                            from app.services.owned_process import probe_native
                            assert asyncio.run(probe_native(item['identity']['native']['distribution'],item['identity']['native']['process']))['state']=='exited'
                if mode in {'bad_birth','bad_nonce','expired_lease','register_failure'}:
                    assert not (api.root/('tool-'+identifier)).with_suffix('.released').exists()
                if mode=='expired_lease':assert (api.root/('late-probe-'+str(next(iter(children.values()))['identity']['host']['pid']))).exists()
                for member in members:assert identity.probe(member)['state']=='exited'
                assert sentinel.poll() is None
                assert api.call('GET','/fixture/job-count/'+str(api.case_id),api_prefix=False)['count']==before
                channel=json.loads((api.root/('channel-'+identifier+'.json')).read_text())
                assert channel['capability'] not in json.dumps(row)
                if mode=='normal':
                    invalid=api.call('GET','/fixture/journal-check/'+identifier,api_prefix=False)['rejected']
                    assert len(invalid)==5
                readbacks.append(row);runs.append({'mode':mode,'request_id':identifier,'members':members,'row':row})
                checks.append(mode);print('PASS native_request_'+mode,flush=True)
        reader=IsolatedServer('native-request-reader',app='request_reader:app',database_url=database_url)
        overrides={key:server.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=server.env['VIBEFLOW_DATABASE_URL']
        readcode='''import json
from sqlalchemy import update
from app.services.durable_events import now
from datetime import timedelta
@app.post('/fixture/expire/{identifier}')
async def expire(identifier:str):
    async with SessionLocal() as db:
        await db.execute(update(NativeRequest).where(NativeRequest.id==identifier).values(lease_expires_at=now()-timedelta(seconds=1)))
        await db.commit()
    return {'ok':True}
'''
        (reader.root/'request_reader.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nPANDOC='+repr(pandoc)+'\n'+CONTROLLED+readcode,encoding='utf-8')
        with reader as api:
            api.token=token
            for row in readbacks:
                persisted=api.call('GET','/fixture/request/'+row['id'],api_prefix=False)
                assert persisted['children']==row['children'] and persisted['phase']==row['phase']
            old=death['row'];api.call('POST','/fixture/expire/'+old['id'],api_prefix=False)
            persisted=api.call('GET','/fixture/request/'+old['id'],api_prefix=False)
            assert persisted['phase']=='recovery_required' and persisted['ended_at'] is None
            assert persisted['children']==old['children'] and persisted['cleanup_verified'] is False
            assert all(c['stop'] is None for c in persisted['children']['children'].values())
            readbacks.append(persisted)
    finally:sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),
        'reader_evidence':str(reader.root),'restart_readback_count':len(readbacks),'jobs_not_allocated':True,
        'concurrent_case_write_while_tool_active':True,'owner_death_no_fabricated_receipt':True,
        'invalid_journal_fields_rejected':invalid}
    (server.root/'native-request-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='native-request-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_request_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_request_[0-9a-f]{32}',name)
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
    print('NATIVE_REQUEST_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
