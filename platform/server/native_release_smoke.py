"""Native child/request release: durable cancellation, leases and send failures."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from professional_launch_smoke import settled
from app.services import process_identity_native as identity
from mpi_scope_smoke import inspect_native
from workflow_migration_smoke import alembic

CONTROLLED='''import asyncio,json,sys
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import SessionLocal,get_db
from app.models import JobAttempt,NativeRequest
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import native_execution as native,native_child_records as children,native_request_records as requests
from app.services import native_request_execution as execution,pandoc_execution,latex_artifacts
from app.services.runtime_config import get_runtime_setting
from app.services.durable_events import now
root=Path(__file__).parent;chosen={};prepared={};background=set()
fail_commit=ContextVar('native_release_commit_fault',default=False)
def setting():return json.loads((root/'setting.json').read_text())
async def barrier(name):
 (root/(name+'.ready')).touch()
 for _ in range(1000):
  if (root/(name+'.continue')).exists():return
  await asyncio.sleep(.02)
 raise TimeoutError('Fixture '+name+' barrier')
execute=native.NativeExecutor._execute
async def executing(self,entry,purpose,parameters,argv,timeout,limit,output):
 selected=setting();job=hasattr(self.owner,'job_id')
 target=('pandoc_parse' if selected['backend']=='pandoc' else 'latex_probe' if selected['caller']=='worker' or not job else 'latex_compile')
 if (job==(selected['scope']=='job') and entry.caller==selected['caller'] and purpose==target and not chosen):
  chosen[entry.id]={'executor':self,'entry':entry,'setting':selected}
  marker=root/'payload.started';resume=root/'payload.continue'
  if self.runtime=='windows':
   argv=[argv[0],'-I','-S','-u',str(root/'observed_native.py'),str(marker),str(resume),selected['action'],*argv]
  else:
   argv=[*argv[:4],'python3','-I','-S','-u',native.wsl_path(root/'observed_native.py'),native.wsl_path(marker),
         native.wsl_path(resume),selected['action'],*argv[4:]]
 return await execute(self,entry,purpose,parameters,argv,timeout,limit,output)
native.NativeExecutor._execute=executing
def install(records):
 register=records.register
 async def registering(owner,nonce,identifier,*args):
  result=await register(owner,nonce,identifier,*args)
  if identifier in chosen:
   selected=chosen[identifier]['setting']
   info={'child_id':identifier,'identity':args[0],**selected}
   info.update({'job_id':owner.job_id} if hasattr(owner,'job_id') else {'request_id':owner.id})
   (root/'selected.json').write_text(json.dumps(info))
   if selected['action']=='cancel_before':await barrier('registered')
   if selected['action']=='expired':
    async with SessionLocal() as db:
     row=await db.get(JobAttempt,owner.attempt_id) if hasattr(owner,'job_id') else await db.get(NativeRequest,owner.id)
     row.lease_expires_at=now()-timedelta(seconds=30)
     await db.commit()
   if selected['action']=='local_close':chosen[identifier]['executor'].stop('cancelled')
  return result
 records.register=registering
 release=records.release
 async def releasing(owner,nonce,identifier,*args):
  selected=chosen.get(identifier,{}).get('setting',{})
  token=fail_commit.set(selected.get('action')=='commit_failure')
  try:return await release(owner,nonce,identifier,*args)
  finally:fail_commit.reset(token)
 records.release=releasing
install(children);install(requests)
original=native.OwnedProcess.release
async def releasing_process(self):
 selected=next((v for v in chosen.values() if v['entry'].owned is self),None)
 if selected and selected['setting']['action']=='release_first':await barrier('release_locked')
 result=await original(self)
 if selected:(root/'release.sent').touch()
 return result
native.OwnedProcess.release=releasing_process
commit=AsyncSession.commit
async def committing(db):
 if fail_commit.get():
  (root/'commit.failed').touch();raise OSError('Injected native release commit failure')
 return await commit(db)
AsyncSession.commit=committing
from app.main import app
@app.post('/fixture/reset')
async def reset(user=Depends(get_current_user)):
 assert not background
 chosen.clear();return {'ok':True}
@app.post('/fixture/prepare/{case_id}/{backend}')
async def prepare(case_id:int,backend:str,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db);user_id=user.id
 from app.storage import _fs_path
 cwd=_fs_path(case.s3_prefix,'.');cwd.mkdir(parents=True,exist_ok=True)
 await db.rollback();ready=asyncio.Event();holder={}
 async def work():
  try:
   profile=({'pandoc':get_runtime_setting('pandoc',PANDOC)} if backend=='pandoc' else
     {'distro':get_runtime_setting('wsl_distro','vibeflow-linux'),'recipe':'pdflatex'})
   async with execution.scope(case_id,user_id,cwd,'report.prepare' if backend=='pandoc' else 'latex.prepare',profile) as executor:
    holder['id']=executor.owner.id;ready.set()
    if backend=='pandoc':await executor.run_sync(pandoc_execution.run,'pandoc_parse',profile['pandoc'],cwd,markdown=b'# Native request release\\n')
    else:await executor.run_sync(latex_artifacts.probe,profile['distro'],'pdflatex')
   prepared[holder['id']]={'ok':True}
  except BaseException as error:
   if 'id' in holder:prepared[holder['id']]={'error':type(error).__name__}
   else:holder['error']=type(error).__name__;ready.set()
 task=asyncio.create_task(work());background.add(task);task.add_done_callback(background.discard)
 await ready.wait();return holder
@app.get('/fixture/request/{identifier}')
async def read(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeRequest,identifier)
 await get_owned_case(row.case_id,user,db,write=False)
 return {**requests.public(row),'result':prepared.get(identifier)}
'''

OBSERVED='''import os,subprocess,sys,time
from pathlib import Path
Path(sys.argv[1]).touch()
if sys.argv[3]=='release_first':
 end=time.monotonic()+20
 while not Path(sys.argv[2]).exists() and time.monotonic()<end:time.sleep(.02)
if os.name=='nt':raise SystemExit(subprocess.call(sys.argv[4:]))
os.execvp(sys.argv[4],sys.argv[4:])
'''

PEER='''import os
from pathlib import Path
os.environ.update(OVERRIDES)
from app.services import jobrunner,distributed_queue
root=Path(__file__).parent;original=jobrunner._persist_status
async def persist(*args,**kwargs):
 (root/'cancel.entered').touch()
 return await original(*args,**kwargs)
jobrunner._persist_status=persist
distributed_queue.distributed_enabled=lambda:True
async def unavailable(job_id):
 (root/'notification.failed').touch()
 raise OSError('Injected native release notification failure')
distributed_queue.cancel=unavailable
from app.main import app
'''


def wait_file(path,seconds=40):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if path.exists():return
        time.sleep(.02)
    raise TimeoutError(str(path))


def wait_request(api,identifier):
    end=time.monotonic()+60
    while time.monotonic()<end:
        row=api.call('GET','/fixture/request/'+identifier,api_prefix=False)
        # The result flag may advance while this GET is still authorizing a
        # previously loaded active row. Wait for the durable request terminal
        # state as well; a finished background task is not that DB observation.
        if row['result'] is not None and row['phase']!='active':return row
        time.sleep(.05)
    raise TimeoutError(json.dumps(row))


def matrix():
    cases=[(backend,'job',caller,action) for backend in ('pandoc','latex') for caller,action in (
        ('worker','normal'),('worker','cancel_before'),('finalizer','cancel_before'),('worker','expired'),('finalizer','commit_failure'))]
    cases += [('pandoc','job','worker','release_first'),('latex','job','finalizer','release_first')]
    cases += [(backend,'request','finalizer',action) for backend in ('pandoc','latex') for action in ('normal','expired','commit_failure','local_close')]
    return cases


def verify(database_url=None,*,cases=None):
    cases=matrix() if cases is None else cases
    assert cases and all(case in matrix() for case in cases)
    api=IsolatedServer('native-release',app='native_release_control:app',database_url=database_url)
    pandoc=str(SERVER.parents[1]/'extracted/share/vibeflow/tools/pandoc/pandoc.exe')
    (api.root/'native_release_control.py').write_text('PANDOC='+repr(pandoc)+'\n'+CONTROLLED,encoding='utf8')
    (api.root/'observed_native.py').write_text(OBSERVED,encoding='utf8')
    checks=[];receipts=[]
    (api.root/'setting.json').write_text(json.dumps(dict(zip(('backend','scope','caller','action'),cases[0]))))
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-u','-c','import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    try:
        with api:
            peer=IsolatedServer('native-release-peer',app='native_release_peer:app',database_url=database_url)
            overrides={k:api.env[k] for k in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
            if database_url is None:overrides['VIBEFLOW_DATABASE_URL']=api.env['VIBEFLOW_DATABASE_URL']
            (peer.root/'native_release_peer.py').write_text(PEER.replace('OVERRIDES',repr(overrides)),encoding='utf8')
            with peer:
                for backend,scope,caller,action in cases:
                    api.create_case('Native release '+backend+' '+scope+' '+caller+' '+action);peer.token=api.token
                    api.call('POST','/fixture/reset',api_prefix=False)
                    for name in ('selected.json','payload.started','payload.continue','registered.ready','registered.continue',
                                 'release_locked.ready','release_locked.continue','release.sent','commit.failed'):
                        (api.root/name).unlink(missing_ok=True)
                    for name in ('cancel.entered','notification.failed'):(peer.root/name).unlink(missing_ok=True)
                    selection={'backend':backend,'scope':scope,'caller':caller,'action':action}
                    (api.root/'setting.json').write_text(json.dumps(selection))
                    source='E-report/report.md' if backend=='pandoc' else 'E-report/latex/main.tex'
                    content='# Native child release\n\nOriginal source remains unchanged.\n' if backend=='pandoc' else '\\documentclass{article}\n\\begin{document}Native release.\\end{document}\n'
                    api.put(source,content)
                    case_root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                    source_hash=hashlib.sha256((case_root/source).read_bytes()).hexdigest()
                    if scope=='job':
                        endpoint='/report:build' if backend=='pandoc' else '/latex:build'
                        body={} if backend=='pandoc' else {'main':source,'recipe':'pdflatex'}
                        started=api.call('POST',api.prefix+endpoint,body,extra_headers={'Idempotency-Key':'native-release'})
                    else:started=api.call('POST',f'/fixture/prepare/{api.case_id}/{backend}',api_prefix=False)
                    cancel=None
                    if action=='cancel_before':
                        wait_file(api.root/'registered.ready')
                        assert not (api.root/'payload.started').exists()
                        cancel=peer.call('POST',f'/jobs/{started["job_id"]}:cancel')
                        assert cancel['ok'] and peer.call('GET',f'/jobs/{started["job_id"]}')['status']=='cancelled'
                        assert not (api.root/'payload.started').exists()
                        (api.root/'registered.continue').touch()
                    elif action=='release_first':
                        wait_file(api.root/'release_locked.ready')
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            pending=pool.submit(peer.call,'POST',f'/jobs/{started["job_id"]}:cancel')
                            wait_file(peer.root/'cancel.entered');time.sleep(.15)
                            assert not pending.done() and not (api.root/'payload.started').exists()
                            (api.root/'release_locked.continue').touch()
                            cancel=pending.result(timeout=20);assert cancel['ok']
                        (api.root/'payload.continue').touch()
                    row=settled(api,started['job_id']) if scope=='job' else wait_request(api,started['id'])
                    selected=json.loads((api.root/'selected.json').read_text())
                    children=row['attempt']['child_processes']['children'] if scope=='job' else row['children']['children']
                    child=children[selected['child_id']]
                    assert child['identity'] is not None and child['stop']['verified'],row
                    expected='succeeded' if action=='normal' else 'cancelled' if action in {'cancel_before','release_first'} else 'failed'
                    assert row['status' if scope=='job' else 'phase']==expected,row
                    if action in {'cancel_before','expired','local_close'}:
                        assert not (api.root/'payload.started').exists() and not (api.root/'release.sent').exists()
                    if action=='normal':assert (api.root/'payload.started').exists()
                    # Release ordering does not promise the OS schedules user
                    # code before a subsequent cancellation can stop the gate.
                    if action=='release_first':assert (api.root/'release.sent').exists()
                    if action=='commit_failure':assert (api.root/'release.sent').exists() and (api.root/'commit.failed').exists()
                    if cancel is not None:assert (peer.root/'notification.failed').exists()
                    assert identity.probe(child['identity']['host'])['state']=='exited'
                    if backend=='latex':
                        assert inspect_native({'probe':[child['identity']['native']['process']]})[0]['state']=='exited'
                    assert hashlib.sha256((case_root/source).read_bytes()).hexdigest()==source_hash
                    if scope=='job':
                        persisted=peer.call('GET',f'/jobs/{started["job_id"]}')
                        assert persisted['attempt']['child_processes']==row['attempt']['child_processes']
                        if cancel is not None:assert not peer.call('POST',f'/jobs/{started["job_id"]}:cancel')['ok']
                    assert sentinel.poll() is None
                    receipts.append({**selection,'selected':selected,'row':row,'cancel':cancel,'input_sha256':source_hash,
                        'payload_started':(api.root/'payload.started').exists(),'external_sentinel_alive':True})
                    checks.append('_'.join((backend,scope,caller,action)))
                    (api.root/'release-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                    print('PASS '+checks[-1],flush=True)
    finally:
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),'peer_evidence':str(peer.root),
            'real_redis_used':False,'marker_wrapper_runs_original_native_helper_argv':True}
    (api.root/'release-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='native-release-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_nativerelease_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_nativerelease_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
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
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('NATIVE_RELEASE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
