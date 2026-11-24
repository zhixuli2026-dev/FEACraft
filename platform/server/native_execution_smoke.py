"""Real owned compiler children, durable gates, parent cancellation and owner death."""
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
from types import SimpleNamespace
import uuid

from testsupport import IsolatedServer,SERVER
from mpi_scope_smoke import inspect_native
from app.services import process_identity_native as identity
from app.services import native_child_records
from workflow_migration_smoke import alembic

NORMAL='\\documentclass{article}\n\\begin{document}Owned native PDF.\\end{document}\n'
BUSY='\\documentclass{article}\n\\begin{document}\\newcount\\spin\\loop\\advance\\spin by 1\\iftrue\\repeat\\end{document}\n'
NOISY='\\documentclass{article}\n\\begin{document}\\loop\\typeout{OWNED-NATIVE-LOG-'+('x'*128)+'}\\iftrue\\repeat\\end{document}\n'
CONTROLLED='''import asyncio,contextvars,json,os,sys
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import native_execution as native,native_child_records as records
from app.services import process_identity_native as identity
root=Path(__file__).parent
owners={}
child_callers={}
unverified_nonces=set()
def case(executor):return json.loads((executor.cwd.parent/'manifest.json').read_text())['source']['case_id']
def mode(case_id):
    path=root/('mode-'+str(case_id))
    return path.read_text() if path.exists() else ''
start=native.NativeExecutor.start
async def starting(self):
    if not hasattr(self.owner,'job_id'):return await start(self)
    result=await start(self);case_id=case(self);owners[self.owner.job_id]=case_id
    (root/('channel-'+str(case_id)+'.json')).write_text(json.dumps({'address':self.address,'capability':self.worker_capability}))
    return result
native.NativeExecutor.start=starting
command=native.NativeExecutor._command
def commands(self,purpose,parameters):
    argv,timeout,limit=command(self,purpose,parameters)
    if not hasattr(self.owner,'job_id'):return argv,timeout,limit
    setting=mode(case(self))
    if purpose=='latex_compile':
        if setting=='worker_timeout':timeout=8
        if setting=='worker_logs':limit=2*1024*1024
    if purpose=='latex_probe' and setting in {'commit_fail','bad_parent','bad_birth'}:
        marker=native.wsl_path(root/('forbidden-payload-'+str(case(self))))
        argv=[*argv[:4],'python3','-I','-S','-c','from pathlib import Path;Path('+repr(marker)+').touch();print("{}")']
    return argv,timeout,limit
native.NativeExecutor._command=commands
execute=native.NativeExecutor._execute
async def executing(self,entry,purpose,parameters,*args):
    if not hasattr(self.owner,'job_id'):return await execute(self,entry,purpose,parameters,*args)
    child_callers[entry.id]=entry.caller
    if entry.caller=='finalizer' and purpose=='latex_compile' and mode(case(self)) in {'finalizer_cancel','finalizer_owner_death'}:
        config=Path(parameters['config'])
        (config.parent/'case'/self.profile['--main']).write_text(BUSY,encoding='utf-8')
    return await execute(self,entry,purpose,parameters,*args)
native.NativeExecutor._execute=executing
register=records.register
fail_commit=contextvars.ContextVar('child_commit_failure',default=False)
async def registering(owner,nonce,identifier,*args):
    setting=mode(owners[owner.job_id]);token=fail_commit.set(setting=='commit_fail')
    if setting=='bad_birth':
        altered=json.loads(json.dumps(args[0]))
        altered['native']['process']['start_ticks']=str(int(altered['native']['process']['start_ticks'])+1)
        args=(altered,*args[1:])
    if setting=='unverified_stop':unverified_nonces.add(args[0]['launch_nonce'])
    try:return await register(owner,'0'*64 if setting=='bad_parent' else nonce,identifier,*args)
    finally:fail_commit.reset(token)
records.register=registering
commit=AsyncSession.commit
async def committing(db):
    if fail_commit.get():raise OSError('injected child registration commit failure')
    return await commit(db)
AsyncSession.commit=committing
finish=records.finish
async def finishing(owner,nonce,identifier,*args):
    if mode(owners[owner.job_id])=='finalizer_receipt_fail' and child_callers[identifier]=='finalizer':
        token=fail_commit.set(True)
        try:return await finish(owner,nonce,identifier,*args)
        finally:fail_commit.reset(token)
    return await finish(owner,nonce,identifier,*args)
records.finish=finishing
reports=native.OwnedProcess._control_reports
def control_reports(self):
    if self.nonce in unverified_nonces:return [],False
    return reports(self)
native.OwnedProcess._control_reports=control_reports
from app.services import job_execution
heartbeat=job_execution.heartbeat
async def beating(owner):
    case_id=owners.get(owner.job_id,-1)
    if mode(case_id)=='ownership_loss' and (root/('lose-owner-'+str(case_id))).exists():
        raise OSError('injected ownership persistence failure')
    return await heartbeat(owner)
job_execution.heartbeat=beating
original=asyncio.create_subprocess_exec
async def spawn(original,*args,**kwargs):
    if len(args)>1 and Path(str(args[1])).name=='registered_latex_worker.py':
        case_id=json.loads((Path(kwargs['cwd']).parent/'manifest.json').read_text())['source']['case_id']
        if mode(case_id)=='worker_exit':args=(sys.executable,str(root/'exit_native_worker.py'),str(case_id),*args[1:])
        elif mode(case_id)=='unsigned_exit':args=(sys.executable,'-I','-S','-c','import os;os._exit(-1)')
    return await original(*args,**kwargs)
from testsupport import intercept_owned_spawn
asyncio.create_subprocess_exec=intercept_owned_spawn(original,spawn)
from app.main import app
@app.on_event('startup')
async def owned_exit():
    (root/'api-identity.json').write_text(json.dumps(identity.observe(os.getpid())))
    async def watch():
        while not (root/'exit-owned-api').exists():await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''
WORKER='''import os,runpy,sys,threading,time
from pathlib import Path
root=Path(__file__).parent;case_id=sys.argv[1];sys.argv=sys.argv[2:]
def watch():
    while not (root/('exit-worker-'+case_id)).exists():time.sleep(.02)
    os._exit(0)
threading.Thread(target=watch,daemon=True).start()
runpy.run_path(sys.argv[0],run_name='__main__')
'''


def wait_job(api,job_id,expected):
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        row=api.call('GET',f'/jobs/{job_id}')
        if row['status']==expected and row['ended_at'] is not None and row['attempt']['phase']==expected:return row
        if row['ended_at'] is not None and row['status'] in {'succeeded','failed','cancelled','interrupted'}:
            if row['status']!=expected:raise AssertionError(row)
        time.sleep(.05)
    raise TimeoutError(json.dumps(row))


def wait_compiler(api,job_id,caller):
    deadline=time.monotonic()+40
    while time.monotonic()<deadline:
        row=api.call('GET',f'/jobs/{job_id}')
        assert row['status']=='running',row
        journal=row['attempt']['child_processes']
        for child in journal['children'].values():
            if child['caller']==caller and child['purpose']=='latex_compile' and child['phase']=='running':
                members=inspect_native({'root':child['identity']['native']['process']})
                if any(item['comm'] in {'pdflatex','pdftex'} for item in members):return row,child,members
        time.sleep(.05)
    raise TimeoutError('The real compiler did not appear under a registered child')


def channel_request(channel,capability,distro):
    port=int(channel['address'].rsplit(':',1)[1])
    request={'schema':'vibeflow.native-call.v1','capability':capability,'purpose':'latex_probe',
        'parameters':{'distro':distro,'recipe':'pdflatex'}}
    with socket.create_connection(('127.0.0.1',port),timeout=5) as sock:
        sock.sendall((json.dumps(request)+'\n').encode())
        with sock.makefile('rb') as stream:return json.loads(stream.readline(16385))


def check_journal(row):
    parent=row['attempt']['process_identity'];value=row['attempt']['child_processes']
    job=SimpleNamespace(id=row['id'],case_id=parent['case_id'])
    attempt=SimpleNamespace(id=row['attempt']['id'],worker_id=parent['worker_id'],
        process_identity_json=json.dumps(parent),child_processes_json=json.dumps(value))
    native_child_records.require_stopped(job,attempt)
    changes={'created_at':'2026-11-02T00:00:00','ended_at':None,'phase':'running',
        'parent_launch_nonce':'0'*64,'request_sha256':'invalid','error':'OSError'}
    checked=[]
    for name,replacement in changes.items():
        invalid=json.loads(json.dumps(value));next(iter(invalid['children'].values()))[name]=replacement
        attempt.child_processes_json=json.dumps(invalid)
        assert native_child_records.public(job,attempt) is None,name
        try:native_child_records.require_stopped(job,attempt)
        except (ValueError,TypeError,KeyError,AttributeError):pass
        else:raise AssertionError('Invalid journal permitted publication: '+name)
        checked.append(name)
    return checked


MODES=('normal','worker_cancel','worker_timeout','worker_logs','finalizer_cancel','worker_exit',
    'commit_fail','bad_parent','bad_birth','unverified_stop','finalizer_receipt_fail','unsigned_exit','ownership_loss','owner_death')


def verify(database_url=None,modes=MODES):
    server=IsolatedServer('native-execution',app='controlled_native_execution:app',database_url=database_url)
    (server.root/'controlled_native_execution.py').write_text('BUSY='+repr(BUSY)+'\n'+CONTROLLED,encoding='utf-8')
    (server.root/'exit_native_worker.py').write_text(WORKER,encoding='utf-8')
    checks=[];runs=[];readbacks=[];journal_checks=[]
    base=getattr(sys,'_base_executable',sys.executable)
    marker='import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'
    sentinels=[subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        for argv in ([base,'-I','-S','-u','-c',marker],['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',marker])]
    assert all(child.stdout.readline().strip()==b'READY' for child in sentinels)
    try:
        with server as api:
            for mode in modes:
                api.create_case('Native '+mode)
                source=NOISY if mode=='worker_logs' else BUSY if mode in {'worker_cancel','worker_timeout','worker_exit','ownership_loss','owner_death'} else NORMAL
                main='E-report/latex/main.tex';api.put(main,source)
                case_root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                digest=hashlib.sha256((case_root/main).read_bytes()).hexdigest()
                (api.root/f'mode-{api.case_id}').write_text(mode)
                submitted=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})
                members=[];observed=None
                owner_death=mode in {'owner_death','finalizer_owner_death'}
                if mode in {'worker_cancel','worker_timeout','finalizer_cancel','worker_exit','ownership_loss','owner_death','finalizer_owner_death'}:
                    row,observed,members=wait_compiler(api,submitted['job_id'],'finalizer' if mode.startswith('finalizer_') else 'worker')
                    channel=json.loads((api.root/f'channel-{api.case_id}.json').read_text())
                    before=set(row['attempt']['child_processes']['children'])
                    if mode in {'worker_cancel','finalizer_cancel'}:
                        assert channel_request(channel,'0'*64,'vibeflow-linux')['type']=='error'
                        assert channel_request(channel,channel['capability'],'wrong-distribution')['type']=='error'
                        if mode=='finalizer_cancel':
                            assert channel_request(channel,channel['capability'],'vibeflow-linux')['type']=='error'
                        assert set(api.call('GET',f'/jobs/{submitted["job_id"]}')['attempt']['child_processes']['children'])==before
                        assert api.call('POST',f'/jobs/{submitted["job_id"]}:cancel',{})['ok']
                    elif mode=='worker_exit':(api.root/f'exit-worker-{api.case_id}').touch()
                    elif mode=='ownership_loss':(api.root/f'lose-owner-{api.case_id}').touch()
                    elif owner_death:
                        api_identity=json.loads((api.root/'api-identity.json').read_text())
                        assert identity.probe(api_identity)['state']=='match'
                        (api.root/'exit-owned-api').touch()
                        for _ in range(100):
                            if identity.probe(api_identity)['state']=='exited':break
                            time.sleep(.05)
                        else:raise AssertionError('Actual API owner did not exit')
                if not owner_death:
                    expected='succeeded' if mode=='normal' else 'cancelled' if mode in {'worker_cancel','finalizer_cancel'} else 'failed'
                    row=wait_job(api,submitted['job_id'],expected)
                    if expected=='failed' and row['attempt']['process_stop']['root_exit_code']==0:
                        assert row['exit_code']==-1,row
                    children=row['attempt']['child_processes']['children']
                    if mode=='normal':
                        assert sorted((c['caller'],c['purpose']) for c in children.values())==[
                            ('finalizer','latex_compile'),('worker','latex_compile'),('worker','latex_probe')]
                        assert all(c['phase']=='stopped' and c['stop']['verified'] for c in children.values())
                        journal_checks=check_journal(row)
                    else:
                        assert not (case_root/'E-report/latex/build/main.pdf').exists()
                        if mode=='finalizer_receipt_fail':
                            failed=[c for c in children.values() if c['caller']=='finalizer']
                            assert len(failed)==1 and failed[0]['phase']=='running' and failed[0]['stop'] is None
                        elif mode=='unverified_stop':
                            assert any(c['phase']=='failed' and c['stop'] is not None and not c['stop']['verified'] for c in children.values())
                        else:
                            assert all(c['phase'] in {'stopped','failed'} for c in children.values()),children
                            assert all(c['stop'] is not None and c['stop']['verified'] for c in children.values()),children
                    if mode in {'worker_timeout','worker_logs'}:
                        assert any(c['stop']['reason']==('timeout' if mode=='worker_timeout' else 'log_limit') for c in children.values())
                    if mode=='ownership_loss':assert any(c['stop']['reason']=='ownership_lost' for c in children.values())
                    if mode=='unsigned_exit':
                        assert row['exit_code']==-1 and row['attempt']['process_stop']['root_exit_code']==0xffffffff
                        assert row['attempt']['process_stop']['verified'] and not children
                    if mode in {'commit_fail','bad_parent','bad_birth'}:
                        assert len(children)==1 and next(iter(children.values()))['phase']=='failed'
                        assert not (api.root/f'forbidden-payload-{api.case_id}').exists()
                    for child in children.values():
                        assert child['parent_launch_nonce']==row['attempt']['process_identity']['launch_nonce']
                        assert child['identity']['job_id']==row['id'] and child['identity']['attempt_id']==row['attempt']['id']
                        assert inspect_native({'probe':[child['identity']['native']['process']]})[0]['state']=='exited'
                    channel=json.loads((api.root/f'channel-{api.case_id}.json').read_text())
                    assert channel['capability'] not in json.dumps(row)
                for _ in range(100):
                    observations=inspect_native({'probe':[item['identity'] for item in members]}) if members else []
                    if all(item['state']=='exited' for item in observations):break
                    time.sleep(.05)
                else:raise AssertionError('An owned compiler or descendant survived cleanup')
                assert hashlib.sha256((case_root/main).read_bytes()).hexdigest()==digest
                assert all(child.poll() is None for child in sentinels)
                checks.append(mode);runs.append({'mode':mode,'job_id':submitted['job_id'],'members':members,
                    'exit_code':row.get('exit_code'),'parent_process_stop':row['attempt']['process_stop'],
                    'child_processes':row['attempt']['child_processes'],'owner_death_has_no_fabricated_terminal_receipt':owner_death})
                readbacks.append((api.token,row))
                (api.root/'native-progress.json').write_text(json.dumps(runs,indent=2)+'\n',encoding='utf-8')
                print('PASS '+mode+'_has_owned_native_identity_cleanup_and_preserved_inputs',flush=True)
        reader=IsolatedServer('native-reader',app='native_readback:app',database_url=database_url)
        overrides={key:server.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        # PostgreSQL already arrives through the harness environment; never
        # write its configured role credentials into generated test sources.
        if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=server.env['VIBEFLOW_DATABASE_URL']
        (reader.root/'native_readback.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf-8')
        with reader as api:
            for token,row in readbacks:
                api.token=token;persisted=api.call('GET',f'/jobs/{row["id"]}')
                # Existing startup reconciliation marks abandoned active jobs
                # interrupted; it must not fabricate native cleanup receipts.
                assert persisted['status']==('interrupted' if row['status']=='running' else row['status'])
                assert persisted['attempt']['child_processes']==row['attempt']['child_processes']
                if row['status']!='running':
                    assert persisted['exit_code']==row['exit_code'] and persisted['ended_at']==row['ended_at']
            if len(readbacks)>1:
                api.token=readbacks[-1][0]
                api.call('GET',f'/jobs/{readbacks[0][1]["id"]}',expect=404)
            print('PASS fresh_api_reads_exact_durable_children_without_replay',flush=True)
    finally:
        for child in sentinels:child.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),
        'reader_evidence':str(reader.root),'restart_readback_count':len(readbacks),'journal_invalid_fields_rejected':journal_checks}
    (server.root/'native-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


def verify_database(database_url=None):
    result=verify(database_url)
    finalizer=verify(database_url,('finalizer_owner_death',))
    result['count']+=finalizer['count'];result['checks']+=finalizer['checks'];result['runs']+=finalizer['runs']
    result['finalizer_owner_death_evidence']=finalizer['evidence']
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='native-execution-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify_database)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_native_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_native_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify_database,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('NATIVE_EXECUTION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
