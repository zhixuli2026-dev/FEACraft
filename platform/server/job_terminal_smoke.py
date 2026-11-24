"""Committed Job results agree across owner/peer APIs, SSE and native evidence."""
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
import threading
import time
import urllib.request
import uuid

from testsupport import IsolatedServer,SERVER
from app.services import process_identity_native as identity
from workflow_migration_smoke import alembic

TEXT='A'*4095+'中'+'B'*61437+'文\n'
CONTROLLED='''import asyncio,json,os,sys
from datetime import timedelta
from pathlib import Path
from sqlalchemy import update
from app.services import jobrunner,job_execution
from app.services import process_identity_native as identity
root=Path(__file__).parent
owners={}
def mode(case_id):
    path=root/('mode-'+str(case_id));return path.read_text() if path.exists() else ''
start=jobrunner.JobProcess.start
async def starting(self):
    case_id=int(self.cwd.name);owners[case_id]=self.owner.job_id
    self.argv=[getattr(sys,'_base_executable',sys.executable),'-I','-S','-u',str(root/'terminal_worker.py')]
    async def finish(status,code):
        (root/('ready-'+str(case_id))).touch()
        while not (root/('release-'+str(case_id))).exists():await asyncio.sleep(.02)
    self.on_finish=finish
    return await start(self)
jobrunner.JobProcess.start=starting
launch=jobrunner.start_job
async def launching(job_id,command,prefix,*args,**kwargs):
    if mode(int(prefix.rstrip('/').rsplit('/',1)[1]))=='queued_cancel':return None
    return await launch(job_id,command,prefix,*args,**kwargs)
jobrunner.start_job=launching
complete=job_execution.finish
async def completing(owner,status,*args,**kwargs):
    case_id=next(key for key,value in owners.items() if value==owner.job_id)
    if mode(case_id) in {'late_failure','fallback_failure'}:status='failed';kwargs['reason']='publication_failed'
    if mode(case_id)=='unverified_cleanup':
        kwargs['process_stop']={**kwargs['process_stop'],'verified':False,'error':'OSError'}
    return await complete(owner,status,*args,**kwargs)
job_execution.finish=completing
from app.services import workflow_fallback
fallback=workflow_fallback.apply_failure_fallback
async def applying(db,case,job):
    if mode(case.id)=='fallback_failure':raise OSError('injected optional post-commit fallback failure')
    return await fallback(db,case,job)
workflow_fallback.apply_failure_fallback=applying
from app.main import app
@app.on_event('startup')
async def watch_owner():
    (root/'api-identity.json').write_text(json.dumps(identity.observe(os.getpid())))
    async def watch():
        while True:
            for marker in root.glob('legacy-seed-*'):
                from app.db import SessionLocal
                from app.models import Job
                case_id=int(marker.name.rsplit('-',1)[1])
                async with SessionLocal() as db:
                    legacy=Job(case_id=case_id,type='shell',command='Legacy terminal',status='failed',exit_code=3)
                    db.add(legacy);await db.commit()
                    (root/('legacy-job-'+str(case_id)+'.json')).write_text(json.dumps({'id':legacy.id}))
                marker.unlink()
            for case_id,job_id in list(owners.items()):
                if (root/('exit-owner-'+str(case_id))).exists():
                    from app.db import SessionLocal
                    from app.models import JobAttempt
                    from app.services.durable_events import now
                    async with SessionLocal() as db:
                        await db.execute(update(JobAttempt).where(JobAttempt.job_id==job_id)
                            .values(lease_expires_at=now()-timedelta(seconds=1)))
                        await db.commit()
                    os._exit(125)
            await asyncio.sleep(.02)
    asyncio.create_task(watch())
'''


class Collector:
    def __init__(self,api,job_id):
        self.url=api.base+f'/api/v1/jobs/{job_id}/log-stream';self.token=api.token
        self.events=[];self.connected=threading.Event();self.logged=threading.Event();self.closed=threading.Event()
        self.response=None;self.failure=None;self.expected_close=False
        self.thread=threading.Thread(target=self.read,daemon=True);self.thread.start()

    def read(self):
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request=urllib.request.Request(self.url,headers={'Authorization':'Bearer '+self.token})
            with opener.open(request,timeout=30) as response:
                self.response=response;self.connected.set()
                for line in response:
                    if line.startswith(b'data: '):
                        value=json.loads(line[6:]);self.events.append(value)
                        if value['type']=='log' and value.get('text'):self.logged.set()
        except Exception as error:
            if not self.expected_close:self.failure=type(error).__name__
        finally:self.closed.set()

    def wait(self):
        assert self.closed.wait(20),'SSE did not close after durable completion'
        self.thread.join(1);assert self.failure is None,self.failure
        return self.events

    def disconnect(self):
        self.expected_close=True
        if self.response is not None:
            try:self.response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError,OSError):pass
        assert self.closed.wait(5),'Viewing connection did not close'


def wait_file(path):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if path.exists():return
        time.sleep(.02)
    raise TimeoutError('Terminal test barrier not reached: '+path.name)


def peer(api):
    result=IsolatedServer('job-terminal-reader')
    result.log=(result.root/'api.log').open('w',encoding='utf-8')
    result.proc=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--app-dir',str(SERVER),
        '--host','127.0.0.1','--port',str(result.port),'--no-access-log'],cwd=api.root,env=api.env,
        stdout=result.log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        if result.proc.poll() is not None:raise AssertionError('Reader API did not start')
        try:
            if result.call('GET','/api/health',api_prefix=False).get('ok'):return result
        except (OSError,ValueError):pass
        time.sleep(.05)
    result.__exit__(TimeoutError,None,None);raise TimeoutError('Terminal reader did not start')


def settled(api,job_id,status):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        row=api.call('GET',f'/jobs/{job_id}')
        if row['status']==status and row['attempt']['phase']==status and row['attempt'].get('process_stop') is not None:
            return row
        time.sleep(.02)
    raise TimeoutError('Terminal transaction did not complete')


def same_result(api,other,job_id,expected,streams,expected_cleanup=True,expected_exit=None):
    one=api.call('GET',f'/jobs/{job_id}');two=other.call('GET',f'/jobs/{job_id}')
    fields=('status','exit_code','ended_at')
    assert {key:one[key] for key in fields}=={key:two[key] for key in fields}
    assert one['status']==expected
    assert one['exit_code']==(expected_exit if expected_exit is not None else 0 if expected=='succeeded' else -1 if expected=='failed' else None)
    for client in (api,other):
        listed=next(row for row in client.call('GET',api.prefix+'/jobs') if row['id']==job_id)
        assert {key:listed[key] for key in fields}=={key:one[key] for key in fields}
        assert client.call('GET',f'/jobs/{job_id}/log')['status']==expected
    for stream in streams:
        events=stream.wait();done=[event for event in events if event['type']=='done']
        assert len(done)==1 and not any(event['type']=='error' for event in events),events
        assert {key:done[0][key] for key in fields}=={key:one[key] for key in fields}
        assert done[0]['cleanup_verified'] is expected_cleanup
        if expected_cleanup is not None:
            assert ''.join(event.get('text','') for event in events if event['type']=='log')==TEXT
    return one


def verify(database_url=None):
    server=IsolatedServer('job-terminal',app='controlled_terminal:app',database_url=database_url)
    (server.root/'controlled_terminal.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'terminal_worker.py').write_text('import sys\nsys.stdout.buffer.write('+repr(TEXT.encode('utf-8'))+')\nsys.stdout.flush()\n',encoding='utf-8')
    checks=[];runs=[]
    with server as api:
        other=peer(api)
        try:
            api.create_case('Legacy nullable completion');other.token=api.token
            (api.root/f'legacy-seed-{api.case_id}').touch()
            wait_file(api.root/f'legacy-job-{api.case_id}.json')
            legacy_id=json.loads((api.root/f'legacy-job-{api.case_id}.json').read_text())['id']
            legacy=same_result(api,other,legacy_id,'failed',[Collector(api,legacy_id),Collector(other,legacy_id)],None,3)
            assert legacy['ended_at'] is None and 'attempt' not in legacy
            checks.append('legacy_nullable_end');runs.append({'mode':'legacy_nullable_end','job_id':legacy_id,'exit_code':3,'ended_at':None})
            print('PASS legacy_nullable_end_preserves_unknown_time_and_cleanup',flush=True)
            for mode in ('normal','late_failure','fallback_failure','unverified_cleanup','remote_cancel','stream_disconnect','queued_cancel','owner_death'):
                api.create_case('Terminal '+mode);other.token=api.token
                (api.root/f'mode-{api.case_id}').write_text(mode)
                submitted=api.call('POST',api.prefix+'/jobs',{'command':'controlled-terminal','runtime':'windows'})
                job_id=submitted['id']
                streams=[]
                if mode!='queued_cancel':wait_file(api.root/f'ready-{api.case_id}')
                count=16 if mode=='normal' else 1
                streams=[Collector(other,job_id) for _ in range(count)]
                if mode!='owner_death' and mode!='queued_cancel':streams.append(Collector(api,job_id))
                for stream in streams:
                    assert stream.connected.wait(10),'SSE request retained the database pool'
                    if mode!='queued_cancel':assert stream.logged.wait(10),'SSE did not deliver the persisted log'
                    assert not stream.closed.is_set(),'Active job was acknowledged before terminal commit'
                live=api.call('GET',f'/jobs/{job_id}')
                assert live['status']==('queued' if mode=='queued_cancel' else 'running') and live['exit_code'] is None and live['ended_at'] is None
                if mode=='owner_death':
                    owner=json.loads((api.root/'api-identity.json').read_text());assert identity.probe(owner)['state']=='match'
                    (api.root/f'exit-owner-{api.case_id}').touch()
                    for _ in range(200):
                        if identity.probe(owner)['state']=='exited':break
                        time.sleep(.02)
                    else:raise AssertionError('Actual terminal owner did not exit')
                    events=streams[0].wait()
                    assert not any(event['type']=='done' for event in events)
                    assert any(event.get('code')=='recovery_required' for event in events)
                    abandoned=other.call('GET',f'/jobs/{job_id}');assert abandoned['status']=='running'
                    reader=peer(api);reader.token=api.token
                    try:
                        preserved=reader.call('GET',f'/jobs/{job_id}')
                        # Startup cannot discard a pending publication merely
                        # because its exact owner exited. N5 recovery retains
                        # finalizing and the SSE recovery-required boundary.
                        assert preserved['status']=='running' and preserved['attempt']['phase']=='finalizing'
                        assert preserved['ended_at'] is None and preserved['attempt']==abandoned['attempt']
                        events=Collector(reader,job_id).wait()
                        assert not any(event['type']=='done' for event in events)
                        assert any(event.get('code')=='recovery_required' for event in events)
                    finally:reader.__exit__(None,None,None)
                    row=preserved
                else:
                    expected='failed' if mode in {'late_failure','fallback_failure','unverified_cleanup'} else 'cancelled' if mode in {'remote_cancel','queued_cancel'} else 'succeeded'
                    if mode in {'remote_cancel','queued_cancel'}:
                        assert other.call('POST',f'/jobs/{job_id}:cancel',{})['ok']
                        for client in (api,other):
                            pending=client.call('GET',f'/jobs/{job_id}')
                            assert pending['status']=='cancelled' and pending['exit_code'] is None
                            assert client.call('GET',f'/jobs/{job_id}/log')['status']=='cancelled'
                        if mode=='remote_cancel':assert all(not stream.closed.is_set() for stream in streams)
                    if mode=='stream_disconnect':
                        streams[0].disconnect();streams=streams[1:]
                        assert api.call('GET',f'/jobs/{job_id}')['status']=='running'
                    if mode!='queued_cancel':
                        (api.root/f'release-{api.case_id}').touch();settled(api,job_id,expected)
                    else:
                        for stream in streams:assert stream.closed.wait(10)
                    cleanup=None if mode=='queued_cancel' else mode!='unverified_cleanup'
                    row=same_result(api,other,job_id,expected,streams,cleanup)
                    if mode=='late_failure':assert row['attempt']['process_stop']['root_exit_code']==0
                    assert not other.call('POST',f'/jobs/{job_id}:cancel',{})['ok']
                    same_result(api,other,job_id,expected,[Collector(api,job_id),Collector(other,job_id)],cleanup)
                checks.append(mode);runs.append({'mode':mode,'job_id':job_id,'status':row['status'],
                    'exit_code':row['exit_code'],'ended_at':row['ended_at']})
                (api.root/'terminal-progress.json').write_text(json.dumps(runs,indent=2)+'\n',encoding='utf-8')
                print('PASS '+mode+'_keeps_committed_results_and_SSE_completion_consistent',flush=True)
        finally:
            for path in api.root.glob('ready-*'):(api.root/path.name.replace('ready-','release-')).touch()
            other.__exit__(None,None,None)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root)}
    (server.root/'terminal-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='job-terminal-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
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
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('JOB_TERMINAL_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
