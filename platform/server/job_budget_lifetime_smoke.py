"""Execution timeout must include post-process finalization, with real API Jobs."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER

CONTROLLED='''import asyncio,contextvars,json,time
from pathlib import Path
from app.services import jobrunner,artifact_publication,attempt_workspace,job_delivery,job_execution,case_validation
from app.services.async_ops import finish_before_cancel
from sqlalchemy.ext.asyncio import AsyncSession
root=Path(__file__).parent
async def no_scanner(stop):await stop.wait()
job_delivery.recover_forever=no_scanner
def mode():return (root/'mode').read_text()
async def delay(seconds=5):
 (root/'held').touch()
 await asyncio.sleep(seconds)
 (root/'drained').touch()
start=jobrunner.start_job
async def starting(*args,**kwargs):
 if mode() in {'hook','cancel_hook'}:
  async def hook(status,code):
   await delay()
  kwargs['on_finish']=hook
 return await start(*args,**kwargs)
jobrunner.start_job=starting
prepare=artifact_publication.prepare_for_finish
async def preparing(*args,**kwargs):
 if mode()=='prepare':await delay()
 return await prepare(*args,**kwargs)
artifact_publication.prepare_for_finish=preparing
workspace=attempt_workspace.runtime_workspace
async def preparing_workspace(owner,*args,**kwargs):
 if mode()=='startup' and not kwargs.get('complete'):
  def copy():
   (root/'held').touch();time.sleep(5);(root/'drained').touch()
  await finish_before_cancel(asyncio.to_thread(copy))
 return await workspace(owner,*args,**kwargs)
attempt_workspace.runtime_workspace=preparing_workspace
late_reply=contextvars.ContextVar('budget_fixture_commit_reply',default=False)
reconcile=job_execution.reconcile_workflow
async def reconciling(db,attempt):
 result=await reconcile(db,attempt)
 if attempt.phase=='succeeded':
  if mode()=='precommit':await delay()
  if mode()=='commit_reply':late_reply.set(True)
 return result
job_execution.reconcile_workflow=reconciling
commit=AsyncSession.commit
async def committing(db):
 await commit(db)
 if late_reply.get():
  late_reply.set(False)
  await delay(12)
AsyncSession.commit=committing
reserve=case_validation.reserve_locked
async def reserving(*args,**kwargs):
 from app.services.case_write import CaseWriteConflict
 try:return await reserve(*args,**kwargs)
 except CaseWriteConflict:
  if mode()=='validation_wait':(root/'validation-wait').touch()
  raise
case_validation.reserve_locked=reserving
release=case_validation.release_current_locked
async def releasing(*args,**kwargs):
 if mode()=='release_wait':await delay()
 return await release(*args,**kwargs)
case_validation.release_current_locked=releasing
from app.main import app
'''


def verify(modes,url=None):
    server=IsolatedServer('job-budget-lifetime',app='controlled_budget:app',database_url=url)
    (server.root/'controlled_budget.py').write_text(CONTROLLED,encoding='utf8')
    checks=[];observations=[]
    with server as api:
        for mode in modes:
            api.create_case('Execution lifetime budget '+mode)
            (api.root/'mode').write_text(mode)
            for name in ('held','drained','payload-started','validation-wait','blocker-started'):(api.root/name).unlink(missing_ok=True)
            blocker=None
            if mode=='validation_wait':
                busy=api.root/'blocker.py'
                busy.write_text('from pathlib import Path\nimport time\nPath('+repr(str(api.root/'blocker-started'))+').touch()\ntime.sleep(30)\n')
                blocker=api.call('POST',api.prefix+'/jobs',{'command':subprocess.list2cmdline([sys.executable,str(busy)]),'timeout_seconds':25})
                deadline=time.monotonic()+10
                while not (api.root/'blocker-started').exists():
                    if time.monotonic()>deadline:raise TimeoutError('Competing case writer not running')
                    time.sleep(.05)
            script=api.root/'payload.py';script.write_text('from pathlib import Path\nimport time\nPath('+repr(str(api.root/'payload-started'))+').touch()\nprint("BUDGET_NATIVE_EXIT_ZERO",flush=True)\n'+('time.sleep(30)\n' if mode=='running' else ''))
            started=time.monotonic()
            seconds=10 if mode in {'normal','commit_reply'} else 6 if mode=='validation_wait' else 2
            row=api.call('POST',api.prefix+'/jobs',{'command':subprocess.list2cmdline([sys.executable,str(script)]),'timeout_seconds':seconds},expect=409 if mode=='startup' else 200)
            if mode=='startup':
                assert row['detail']['code']=='resource_limit',row
                row={'id':row['detail']['job_id']}
            if mode=='cancel_hook':
                deadline=time.monotonic()+10
                while not (api.root/'held').exists():
                    if time.monotonic()>deadline:raise TimeoutError('Finalizer not entered')
                    time.sleep(.02)
                assert api.call('POST',f'/jobs/{row["id"]}:cancel')['ok']
            deadline=time.monotonic()+30
            while True:
                final=api.call('GET',f'/jobs/{row["id"]}')
                if (final['status'] not in {'queued','running'} and not final['completion_pending']
                        and final['attempt']['phase']==final['status']
                        and (mode not in {'cancel_hook','commit_reply'} or (api.root/'drained').exists())):break
                if time.monotonic()>deadline:raise TimeoutError('Budget attempt did not settle')
                time.sleep(.1)
            observed={'mode':mode,'elapsed':time.monotonic()-started,'status':final['status'],
                'reason':final['attempt']['reason_code'],'held':(api.root/'held').exists(),
                'drained':(api.root/'drained').exists(),'job':final}
            observations.append(observed)
            (api.root/'budget-observations.json').write_text(json.dumps(observations,indent=2),encoding='utf8')
            print(json.dumps({k:v for k,v in observed.items() if k!='job'}),flush=True)
            if mode not in {'normal','running','validation_wait'}:assert observed['held'] and observed['drained']
            expected='succeeded' if mode in {'normal','commit_reply'} else 'cancelled' if mode=='cancel_hook' else 'failed'
            assert final['status']==expected, 'Execution lifetime budget or committed result is incorrect'
            if expected=='failed':assert final['attempt']['reason_code']=='resource_limit',final['attempt']['reason_code']
            if mode=='startup':
                assert final['attempt']['process_identity'] is None and not (api.root/'payload-started').exists()
            else:assert final['attempt']['process_stop']['verified']
            if blocker is not None:
                assert (api.root/'validation-wait').exists()
                assert final['attempt']['process_stop']['reason']=='root_exit'
                assert api.call('GET',f'/jobs/{blocker["id"]}')['status']=='running'
                assert api.call('POST',f'/jobs/{blocker["id"]}:cancel')['ok']
                deadline=time.monotonic()+15
                while True:
                    stopped=api.call('GET',f'/jobs/{blocker["id"]}')
                    if stopped['attempt']['phase']=='cancelled' and stopped['attempt']['process_stop'] is not None:break
                    if time.monotonic()>deadline:raise TimeoutError('Competing writer did not drain')
                    time.sleep(.1)
                assert stopped['attempt']['process_stop']['verified']
            checks.append(mode+'_preserves_deadline_drain_and_terminal_semantics')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(server.root)}
    (server.root/'budget-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--modes',default='normal,hook,prepare,startup,precommit,commit_reply,cancel_hook,running,validation_wait,release_wait')
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='budget-lifetime-evidence-',dir=SERVER.parents[1]/'tmp'))
    modes=args.modes.split(',');results={'sqlite':await asyncio.to_thread(verify,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_budget_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_budget_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,modes,target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('BUDGET_LIFETIME_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
