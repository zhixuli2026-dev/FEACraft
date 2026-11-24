"""Real Pandoc stdin, parent-owned children, transport failures and API death."""
import argparse
import asyncio
import hashlib
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
from native_execution_smoke import wait_job,check_journal
from app.services import process_identity_native as identity
from workflow_migration_smoke import alembic

MARKDOWN='# Owned 中文 report\n\nPandoc stdin remains document data.\n\n`{"action":"stop"}`\n'
CONTROLLED='''import asyncio,contextvars,json,os,socket,sys,threading,time
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import native_execution as native,native_child_records as records,pandoc_execution
from app.services import process_identity_native as identity
from app.services import job_execution,artifact_publication
root=Path(__file__).parent
owners={};children={};channels={}
def case(executor):return json.loads((executor.cwd.parent/'manifest.json').read_text())['source']['case_id']
def mode(case_id):
    path=root/('mode-'+str(case_id));return path.read_text() if path.exists() else ''
start=native.NativeExecutor.start
async def starting(self):
    if not hasattr(self.owner,'job_id'):return await start(self)
    result=await start(self);case_id=case(self);owners[self.owner.job_id]=case_id
    channels[int(self.address.rsplit(':',1)[1])]=case_id
    (root/('channel-'+str(case_id)+'.json')).write_text(json.dumps({'address':self.address,'capability':self.worker_capability}))
    return result
native.NativeExecutor.start=starting
execute=native.NativeExecutor._execute
async def executing(self,entry,purpose,parameters,argv,timeout,limit,output):
    if not hasattr(self.owner,'job_id'):return await execute(self,entry,purpose,parameters,argv,timeout,limit,output)
    case_id=case(self);setting=mode(case_id)
    children[entry.id]=(case_id,entry.caller,entry.owned,parameters)
    entry.owned.test_case_id=case_id
    entry.owned.test_caller=entry.caller
    hold=(purpose=='pandoc_parse' and ((entry.caller=='worker' and setting in
        {'worker_cancel','worker_timeout','worker_disconnect','worker_exit','owner_death'}) or
        (entry.caller=='finalizer' and setting in {'finalizer_cancel','finalizer_disconnect','finalizer_owner_death'})))
    if hold:
        argv=[argv[0],'-I','-S','-u',str(root/'held_pandoc.py'),str(case_id),entry.id,self.profile['--pandoc']]
        if setting=='worker_timeout':timeout=4
        (root/('request-'+str(case_id)+'.json')).write_text(json.dumps({'purpose':purpose,'parameters':parameters}))
    if setting in {'commit_fail','bad_birth'}:
        argv=[argv[0],'-I','-S','-c','from pathlib import Path;Path('+repr(str(root/('forbidden-'+str(case_id))))+').touch()']
    if setting=='worker_overflow':
        code='import runpy,sys;from pathlib import Path;f=runpy.run_path(sys.argv[1])["execute"];f.__globals__["STDOUT_LIMIT"]=1024;f(Path(sys.argv[2]),sys.argv[3])'
        argv=[argv[0],'-I','-S','-u','-c',code,str(pandoc_execution.NATIVE),parameters['config'],parameters['sha256']]
        send=output
        async def observed(data):
            with (root/('helper-console-'+str(case_id))).open('ab') as stream:stream.write(data)
            await send(data)
        output=observed
    return await execute(self,entry,purpose,parameters,argv,timeout,limit,output)
native.NativeExecutor._execute=executing
register=records.register
fail_commit=contextvars.ContextVar('pandoc_commit_failure',default=False)
async def registering(owner,nonce,identifier,*args):
    case_id=owners[owner.job_id];setting=mode(case_id);token=fail_commit.set(setting=='commit_fail')
    try:
        if setting=='bad_birth':
            altered=json.loads(json.dumps(args[0]));altered['host']['creation_time_100ns']=str(int(altered['host']['creation_time_100ns'])+1)
            args=(altered,*args[1:])
        result=await register(owner,nonce,identifier,*args)
    finally:fail_commit.reset(token)
    if setting=='config_changed':
        with Path(children[identifier][3]['config']).open('ab') as stream:stream.write(b' ')
    return result
records.register=registering
commit=AsyncSession.commit
async def committing(db):
    if fail_commit.get():raise OSError('injected Pandoc child receipt commit failure')
    return await commit(db)
AsyncSession.commit=committing
settle=job_execution.settle_visibility
async def settling(owner,*args):
    token=fail_commit.set(mode(owners[owner.job_id])=='visibility_receipt_fail')
    try:return await settle(owner,*args)
    finally:fail_commit.reset(token)
job_execution.settle_visibility=settling
repair=artifact_publication.repair_aliases
async def repairing(owner):
    case_id=owners[owner.job_id];setting=mode(case_id)
    if setting=='alias_hold':
        (root/('alias-waiting-'+str(case_id))).touch()
        while not (root/('release-alias-'+str(case_id))).exists():await asyncio.sleep(.02)
    if setting=='alias_failure':raise OSError('injected compatibility alias failure')
    return await repair(owner)
artifact_publication.repair_aliases=repairing
finish=records.finish
async def finishing(owner,nonce,identifier,*args):
    case_id,caller,owned,parameters=children[identifier]
    folder=Path(parameters['config']).parent
    observed={name:(folder/name).stat().st_size for name in ('stdout.bin','stderr.bin') if (folder/name).exists()}
    (root/('streams-'+str(case_id)+'.json')).write_text(json.dumps(observed))
    if mode(case_id)=='finalizer_receipt_fail' and caller=='finalizer':
        token=fail_commit.set(True)
        try:return await finish(owner,nonce,identifier,*args)
        finally:fail_commit.reset(token)
    return await finish(owner,nonce,identifier,*args)
records.finish=finishing
reports=native.OwnedProcess._control_reports
def control_reports(self):
    if mode(getattr(self,'test_case_id',-1))=='unverified_stop':return [],False
    return reports(self)
native.OwnedProcess._control_reports=control_reports
stop=native.OwnedProcess.stop
async def stopping(self,*args,**kwargs):
    case_id=getattr(self,'test_case_id',-1)
    if mode(case_id)=='finalizer_disconnect' and getattr(self,'test_caller',None)=='finalizer':
        (root/('cleanup-waiting-'+str(case_id))).touch()
        while not (root/('release-cleanup-'+str(case_id))).exists():await asyncio.sleep(.02)
    return await stop(self,*args,**kwargs)
native.OwnedProcess.stop=stopping
connect=socket.create_connection
def connecting(address,*args,**kwargs):
    connection=connect(address,*args,**kwargs);case_id=channels.get(address[1],-1)
    if mode(case_id)=='finalizer_disconnect':
        def watch():
            while not (root/('disconnect-'+str(case_id))).exists():time.sleep(.01)
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        threading.Thread(target=watch,daemon=True).start()
    return connection
socket.create_connection=connecting
original=asyncio.create_subprocess_exec
async def spawn(original,*args,**kwargs):
    if len(args)>1 and Path(str(args[1])).name=='registered_report_worker.py':
        case_id=json.loads((Path(kwargs['cwd']).parent/'manifest.json').read_text())['source']['case_id']
        if mode(case_id) in {'worker_exit','worker_disconnect','worker_error','worker_overflow'}:
            args=(sys.executable,str(root/'controlled_pandoc_worker.py'),str(case_id),*args[1:])
    return await original(*args,**kwargs)
from testsupport import intercept_owned_spawn
asyncio.create_subprocess_exec=intercept_owned_spawn(original,spawn)
from app.main import app
from fastapi import Depends
from sqlalchemy import select
from app.db import SessionLocal
from app.models import User,Job,DurableEvent,EventOutbox
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
@app.post('/api/v1/fixture/batch-view/{job_id}')
async def batch_view(job_id:int,user:User=Depends(get_current_user)):
    from app import storage
    from app.services.case_write import coordinate
    from app.services.durable_events import now
    async with SessionLocal() as db:
        job=await db.get(Job,job_id);case=await get_owned_case(job.case_id,user,db)
        batch_id=format(job_id,'032x')
        manifest={'schema':'vibeflow.job-batch.v1','id':batch_id,'case_id':case.id,
            'created_at':now().isoformat(),'runtime':'windows','max_concurrent':1,'job_ids':[job_id]}
        async with coordinate(db,case):
            storage.put_text(case.s3_prefix,'.vibeflow/job-batches/'+batch_id+'.json',json.dumps(manifest))
            storage.put_text(case.s3_prefix,'.vibeflow/job-batches/index.json',json.dumps([batch_id]))
        await db.commit()
        return {'id':batch_id}
@app.get('/api/v1/fixture/completion-event/{job_id}')
async def completion_event(job_id:int,user:User=Depends(get_current_user)):
    async with SessionLocal() as db:
        job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
        rows=await db.execute(select(DurableEvent,EventOutbox).join(EventOutbox,EventOutbox.event_id==DurableEvent.id)
            .where(DurableEvent.case_id==job.case_id,DurableEvent.type=='jobs.updated'))
        return [{'id':e.id,'status':o.status,'error':o.last_error} for e,o in rows
            if json.loads(e.payload_json).get('job_id')==job_id and json.loads(e.payload_json).get('status')=='succeeded']
@app.on_event('startup')
async def owned_exit():
    (root/'api-identity.json').write_text(json.dumps(identity.observe(os.getpid())))
    async def watch():
        while not (root/'exit-owned-api').exists():
            for job_id,case_id in list(owners.items()):
                marker=root/('expire-visibility-'+str(case_id))
                if marker.exists():
                    from datetime import timedelta
                    from sqlalchemy import update
                    from app.db import SessionLocal
                    from app.models import JobAttempt
                    from app.services.durable_events import now
                    async with SessionLocal() as db:
                        await db.execute(update(JobAttempt).where(JobAttempt.job_id==job_id).values(lease_expires_at=now()-timedelta(seconds=1)))
                        await db.commit()
                    marker.unlink()
            await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''
HELD='''import json,os,signal,subprocess,sys,time
from pathlib import Path
root=Path(__file__).parent;case_id=sys.argv[1];child_id=sys.argv[2]
signal.signal(signal.SIGBREAK,signal.SIG_IGN)
tool=subprocess.Popen([sys.argv[3],'--from=markdown','--to=json'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
(root/('tool-'+case_id+'.json')).write_text(json.dumps({'helper_pid':os.getpid(),'tool_pid':tool.pid,'child_id':child_id}))
while tool.poll() is None:time.sleep(.02)
raise SystemExit(tool.returncode)
'''
WORKER='''import os,runpy,socket,sys,threading,time
from pathlib import Path
root=Path(__file__).parent;case_id=sys.argv[1];sys.argv=sys.argv[2:]
sys.path.insert(0,str(Path(sys.argv[0]).resolve().parents[2]))
setting=(root/('mode-'+case_id)).read_text()
if setting=='worker_exit':
    def watch():
        while not (root/('exit-worker-'+case_id)).exists():time.sleep(.01)
        os._exit(0)
    threading.Thread(target=watch,daemon=True).start()
if setting=='worker_disconnect':
    connect=socket.create_connection
    def connecting(*args,**kwargs):
        connection=connect(*args,**kwargs)
        def watch():
            while not (root/('disconnect-'+case_id)).exists():time.sleep(.01)
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        threading.Thread(target=watch,daemon=True).start();return connection
    socket.create_connection=connecting
if setting in {'worker_error','worker_overflow'}:
    from app.services import pandoc_execution
    run=pandoc_execution.run
    def running(purpose,pandoc,root,**kwargs):
        if purpose=='pandoc_parse':kwargs['markdown']=(b'---\\nhello: [\\n---\\n' if setting=='worker_error' else b'x\\n\\n'*200)
        return run(purpose,pandoc,root,**kwargs)
    pandoc_execution.run=running
runpy.run_path(sys.argv[0],run_name='__main__')
'''
MODES=('normal','alias_hold','alias_failure','visibility_receipt_fail','worker_cancel','worker_timeout','worker_disconnect','finalizer_cancel','finalizer_disconnect',
    'worker_exit','commit_fail','bad_birth','config_changed','unverified_stop','finalizer_receipt_fail',
    'worker_error','worker_overflow','owner_death')


def wait_file(path,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if path.exists():return
        time.sleep(.02)
    raise TimeoutError('Test barrier was not reached: '+path.name)


def request(channel,value):
    port=int(channel['address'].rsplit(':',1)[1])
    with socket.create_connection(('127.0.0.1',port),timeout=5) as sock:
        sock.sendall((json.dumps(value)+'\n').encode())
        with sock.makefile('rb') as stream:return json.loads(stream.readline(16385))


def verify(database_url=None,modes=MODES):
    server=IsolatedServer('pandoc-execution',app='controlled_pandoc:app',database_url=database_url)
    for name,value in [('controlled_pandoc.py',CONTROLLED),('held_pandoc.py',HELD),('controlled_pandoc_worker.py',WORKER)]:
        (server.root/name).write_text(value,encoding='utf-8')
    checks=[];runs=[];readbacks=[];journal_checks=[]
    sentinel=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-I','-S','-u','-c',
        'import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    try:
        with server as api:
            for mode in modes:
                api.create_case('Pandoc '+mode);api.put('E-report/report.md',MARKDOWN)
                case_root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                digest=hashlib.sha256((case_root/'E-report/report.md').read_bytes()).hexdigest()
                (api.root/f'mode-{api.case_id}').write_text(mode)
                started=api.call('POST',api.prefix+'/report:build',{},extra_headers={'Idempotency-Key':mode})
                members=[];owner_death=mode in {'owner_death','finalizer_owner_death'}
                visibility_failed=mode=='visibility_receipt_fail'
                held=mode in {'worker_cancel','worker_timeout','worker_disconnect','finalizer_cancel','finalizer_disconnect',
                    'worker_exit','owner_death','finalizer_owner_death'}
                if held:
                    path=api.root/f'tool-{api.case_id}.json';wait_file(path)
                    tool=json.loads(path.read_text());members=[identity.observe(tool[key]) for key in ('helper_pid','tool_pid')]
                    assert all(identity.probe(item)['state']=='match' for item in members)
                    row=api.call('GET',f'/jobs/{started["job_id"]}')
                    child=row['attempt']['child_processes']['children'][tool['child_id']]
                    assert child['phase']=='running' and child['purpose']=='pandoc_parse' and child['identity']['native'] is None
                    if mode in {'worker_cancel','finalizer_cancel'}:
                        channel=json.loads((api.root/f'channel-{api.case_id}.json').read_text())
                        invocation=json.loads((api.root/f'request-{api.case_id}.json').read_text())
                        good={'schema':'vibeflow.native-call.v1','capability':channel['capability'],**invocation}
                        before=set(row['attempt']['child_processes']['children'])
                        for bad in [{**good,'capability':'0'*64},{**good,'purpose':'latex_probe'},
                                {**good,'parameters':{**good['parameters'],'sha256':'0'*64}}]:
                            assert request(channel,bad)['type']=='error'
                        assert set(api.call('GET',f'/jobs/{started["job_id"]}')['attempt']['child_processes']['children'])==before
                        assert api.call('POST',f'/jobs/{started["job_id"]}:cancel',{})['ok']
                    elif mode in {'worker_disconnect','finalizer_disconnect'}:
                        (api.root/f'disconnect-{api.case_id}').touch()
                        if mode=='finalizer_disconnect':
                            wait_file(api.root/f'cleanup-waiting-{api.case_id}')
                            pending=api.call('GET',f'/jobs/{started["job_id"]}')
                            assert pending['status']=='running' and pending['ended_at'] is None
                            assert identity.probe(members[1])['state']=='match'
                            (api.root/f'release-cleanup-{api.case_id}').touch()
                    elif mode=='worker_exit':(api.root/f'exit-worker-{api.case_id}').touch()
                    elif owner_death:
                        owner=json.loads((api.root/'api-identity.json').read_text());assert identity.probe(owner)['state']=='match'
                        (api.root/'exit-owned-api').touch()
                        for _ in range(100):
                            if identity.probe(owner)['state']=='exited':break
                            time.sleep(.02)
                        else:raise AssertionError('Actual Pandoc API owner did not exit')
                if mode in {'alias_hold','visibility_receipt_fail'}:
                    from job_terminal_smoke import peer,Collector
                    if mode=='alias_hold':wait_file(api.root/f'alias-waiting-{api.case_id}')
                    else:
                        deadline=time.monotonic()+30
                        while time.monotonic()<deadline:
                            pending=api.call('GET',f'/jobs/{started["job_id"]}')
                            if pending['completion_error']:break
                            time.sleep(.02)
                        else:raise AssertionError('Visibility receipt failure was not observed')
                    viewer=peer(api);viewer.token=api.token
                    try:
                        # Historical batch views must apply the same completion
                        # visibility as single-job queries, including a report member.
                        batch=api.call('POST',f'/fixture/batch-view/{started["job_id"]}')['id']
                        for client in (api,viewer):
                            pending=client.call('GET',f'/jobs/{started["job_id"]}')
                            assert pending['status']=='running' and pending['completion_pending'] and pending['attempt']['phase']=='succeeded'
                            assert not client.call('POST',f'/jobs/{started["job_id"]}:cancel',{})['ok']
                            replay=client.call('POST',api.prefix+'/report:build',{},extra_headers={'Idempotency-Key':mode})
                            assert replay['job_id']==started['job_id'] and replay['status']=='running'
                            assert client.call('GET',f'/jobs/{started["job_id"]}/log')['status']=='running'
                            listed=next(item for item in client.call('GET',api.prefix+'/jobs') if item['id']==started['job_id'])
                            assert listed['status']=='running' and listed['completion_pending']
                            grouped=client.call('GET',api.prefix+'/job-batches/'+batch)
                            assert grouped['status']=='running' and grouped['active']==1 and grouped['counts']['succeeded']==0
                            assert grouped['jobs'][0]['completion_pending']
                            assert client.call('GET',api.prefix+'/job-batches')[0]['status']=='running'
                        deadline=time.monotonic()+10
                        while time.monotonic()<deadline:
                            notices=api.call('GET',f'/fixture/completion-event/{started["job_id"]}')
                            if notices and notices[0]['error']=='RuntimeError':break
                            time.sleep(.05)
                        else:raise AssertionError(('Success event did not wait for completion visibility',notices))
                        assert notices[0]['status']!='delivered',notices
                        stream=Collector(viewer,started['job_id']);assert stream.logged.wait(10) and not stream.closed.is_set()
                        if mode=='alias_hold':
                            assert not (case_root/'E-report/build/report.html').exists()
                            (api.root/f'release-alias-{api.case_id}').touch()
                            events=stream.wait();assert any(event['type']=='done' for event in events)
                            deadline=time.monotonic()+10
                            while time.monotonic()<deadline:
                                notices=api.call('GET',f'/fixture/completion-event/{started["job_id"]}')
                                if notices[0]['status']=='delivered':break
                                time.sleep(.05)
                            else:raise AssertionError(notices)
                            assert viewer.call('GET',api.prefix+'/job-batches/'+batch)['status']=='succeeded'
                        else:
                            assert b'Pandoc stdin remains document data' in api.call('GET',api.prefix+'/report/artifact?path=E-report%2Fbuild%2Freport.html',raw=True)
                            local=Collector(api,started['job_id']).wait();assert any(event.get('code')=='persistence_failed' for event in local)
                            (api.root/f'expire-visibility-{api.case_id}').touch()
                            events=stream.wait();assert not any(event['type']=='done' for event in events)
                            assert any(event.get('code')=='recovery_required' for event in events)
                            row=api.call('GET',f'/jobs/{started["job_id"]}')
                    finally:viewer.__exit__(None,None,None)
                if not owner_death and not visibility_failed:
                    expected='succeeded' if mode in {'normal','alias_hold','alias_failure'} else 'cancelled' if mode in {'worker_cancel','finalizer_cancel'} else 'failed'
                    row=wait_job(api,started['job_id'],expected)
                    if expected=='failed' and row['attempt']['process_stop']['root_exit_code']==0:
                        assert row['exit_code']==-1,row
                    children=row['attempt']['child_processes']['children']
                    if mode=='normal':
                        assert len(children)==8 and all(c['phase']=='stopped' and c['stop']['verified'] for c in children.values())
                        assert sorted(c['purpose'] for c in children.values()).count('pandoc_compile')==4
                        assert b'Pandoc stdin remains document data' in (case_root/'E-report/build/report.html').read_bytes()
                        journal_checks=check_journal(row)
                    elif mode=='alias_hold':assert (case_root/'E-report/build/report.html').exists()
                    elif mode=='alias_failure':
                        assert row['attempt']['outputs']['completion']['alias_error']=='OSError'
                        assert b'Pandoc stdin remains document data' in api.call('GET',api.prefix+'/report/artifact?path=E-report%2Fbuild%2Freport.html',raw=True)
                    else:assert not (case_root/'E-report/build/report.html').exists()
                    if mode=='finalizer_receipt_fail':
                        assert any(c['caller']=='finalizer' and c['phase']=='running' and c['stop'] is None for c in children.values())
                    elif mode=='unverified_stop':assert any(c['stop'] is not None and not c['stop']['verified'] for c in children.values())
                    else:
                        assert all(c['phase'] in {'stopped','failed'} and c['stop']['verified'] for c in children.values())
                    if mode in {'commit_fail','bad_birth'}:
                        assert len(children)==1 and not (api.root/f'forbidden-{api.case_id}').exists()
                    if mode=='config_changed':assert len(children)==1 and next(iter(children.values()))['stop']['root_exit_code']!=0
                    if mode=='worker_timeout':assert any(c['stop']['reason']=='timeout' for c in children.values())
                    if mode=='worker_error':assert 'YAML' in row['log_backlog'],row['log_backlog']
                    if mode=='worker_overflow':
                        sizes=json.loads((api.root/f'streams-{api.case_id}.json').read_text())
                        assert sizes['stdout.bin']<=1024 and sizes['stderr.bin']<=1024*1024
                        assert b'Pandoc streams did not complete within their bounds' in (api.root/f'helper-console-{api.case_id}').read_bytes()
                    for child in children.values():
                        assert child['identity']['runtime']=='windows' and child['identity']['native'] is None
                        assert identity.probe(child['identity']['host'])['state']=='exited'
                    channel=json.loads((api.root/f'channel-{api.case_id}.json').read_text())
                    assert channel['capability'] not in json.dumps(row)
                for _ in range(200):
                    if all(identity.probe(item)['state']=='exited' for item in members):break
                    time.sleep(.02)
                else:raise AssertionError('An owned Pandoc process survived cleanup')
                assert sentinel.poll() is None
                assert hashlib.sha256((case_root/'E-report/report.md').read_bytes()).hexdigest()==digest
                checks.append(mode);runs.append({'mode':mode,'job_id':started['job_id'],'members':members,
                    'child_processes':row['attempt']['child_processes'],'owner_death_has_no_fabricated_receipt':owner_death})
                readbacks.append((api.token,row))
                (api.root/'pandoc-progress.json').write_text(json.dumps(runs,indent=2)+'\n',encoding='utf-8')
                print('PASS '+mode+'_owns_Pandoc_stdin_processes_and_waits_for_cleanup',flush=True)
        reader=IsolatedServer('pandoc-reader',app='pandoc_readback:app',database_url=database_url)
        overrides={key:server.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=server.env['VIBEFLOW_DATABASE_URL']
        (reader.root/'pandoc_readback.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf-8')
        with reader as api:
            for token,row in readbacks:
                api.token=token;persisted=api.call('GET',f'/jobs/{row["id"]}')
                expected_status='running' if row.get('completion_pending') else 'interrupted' if row['status']=='running' else row['status']
                assert persisted['status']==expected_status
                assert persisted['attempt']['child_processes']==row['attempt']['child_processes']
                if row['status']!='running':
                    assert persisted['exit_code']==row['exit_code'] and persisted['ended_at']==row['ended_at']
            if len(readbacks)>1:
                api.token=readbacks[-1][0];api.call('GET',f'/jobs/{readbacks[0][1]["id"]}',expect=404)
    finally:sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),
        'reader_evidence':str(reader.root),'restart_readback_count':len(readbacks),'journal_invalid_fields_rejected':journal_checks}
    (server.root/'pandoc-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


def verify_database(database_url=None):
    result=verify(database_url);finalizer=verify(database_url,('finalizer_owner_death',))
    result['count']+=finalizer['count'];result['checks']+=finalizer['checks'];result['runs']+=finalizer['runs']
    result['finalizer_owner_death_evidence']=finalizer['evidence']
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--modes',nargs='+',choices=(*MODES,'finalizer_owner_death'));args=parser.parse_args()
    run=lambda url=None:verify(url,tuple(args.modes)) if args.modes else verify_database(url)
    report=Path(tempfile.mkdtemp(prefix='pandoc-execution-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_pandoc_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_pandoc_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(run,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('PANDOC_EXECUTION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
