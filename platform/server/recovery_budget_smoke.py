"""API death preserves deadlines and drained output usage without native replay."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from job_release_smoke import wait_file
from publication_finalizing_smoke import CONTROLLED as FINAL,peer
from publication_preparation_recovery_smoke import CONTROLLED as PREPARATION
from app.services import process_identity_native as identity

CONFIG='''
from app.services.report_handler import ReportHandler
prepare=ReportHandler.prepare
async def preparing(self,*args,budget,**kwargs):
 return await prepare(self,*args,budget={**budget,'timeout_seconds':60,'max_log_bytes':65536},**kwargs)
ReportHandler.prepare=preparing
@app.post('/api/v1/fixture/output-proof/{job_id}')
async def change_output_proof(job_id:int,body:dict,user:User=Depends(get_current_user)):
 from app.services.durable_events import lock_case_execution,json_text
 from app.services.job_execution import attempt_event
 from app.models import DurableEvent
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  await lock_case_execution(db,job.case_id)
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  event=await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type=='attempt.output_drained'))
  value=json.loads(event.payload_json);mode=body['mode']
  if mode=='missing_usage':event.type='fixture.removed_output_proof'
  elif mode=='duplicate_usage':await attempt_event(db,job,attempt,'attempt.output_drained',value)
  else:
   if mode=='corrupt_usage':value['usage']['observed_output_bytes']=True
   elif mode=='wrong_binding':value['worker_sha256']='0'*64
   else:raise ValueError('Unknown fixture mutation')
   event.payload_json=json_text(value)
  await db.commit()
 return {'ok':True}
'''
MARKER="""from app.services.execution_budget import current
 (root/'budget-deadline.json').write_text(json.dumps({'at':time.time(),'remaining':current().remaining()}))
 (root/'ready').touch()"""
MODES=('prepared_normal','prebundle_normal','prepared_expired','prebundle_expired',
       'missing_usage','duplicate_usage','corrupt_usage','wrong_binding','two_peers',
       'verification_expires','prebundle_copy_expires')

EXPIRE_DURING_WORK='''
import time
from pathlib import Path
from app.services import execution_budget,publication_finalizing,publication_preparation_recovery
module=publication_preparation_recovery if COPY_STAGE else publication_finalizing
name='resume_copy' if COPY_STAGE else '_prepared'
original=getattr(module,name)
def delayed(*args):
 result=original(*args)
 budget=execution_budget.current()
 assert budget is not None and budget.log_bytes>0
 time.sleep(budget.remaining()+.2)
 Path(__file__).with_suffix('.budget-drained').touch()
 return result
setattr(module,name,delayed)
'''


def require_dead_owner_drain(row):
    checkpoint=row['checkpoint']
    assert checkpoint['schema']=='vibeflow.job-write-checkpoint.v2' and checkpoint['state']=='stopped'
    assert checkpoint['attempt_id']==row['attempt_id']
    assert checkpoint['stop']==row['output']['process_stop'] and checkpoint['stop']['verified']
    proof=checkpoint['recovery']
    assert proof['schema']=='vibeflow.job-drain-recovery.v1'
    assert proof['owner_observation']=={'state':'exited','reason':'process_exited'}
    worker=json.loads(proof['worker_identity_json'])
    assert identity.probe(worker['process'])['state']=='exited'
    assert sum(e['type']=='attempt.content_drain_recovered' for e in row['events'])==1
    assert not any(e['type']=='attempt.bundle_copy_drained' for e in row['events'])


def without_later_drain(row):
    return {**row,'checkpoint':{},'events':[e for e in row['events'] if e['type']!='attempt.content_drain_recovered']}


def verify(modes,url=None):
    checks=[];runs=[]
    for mode in modes:
        prebundle=mode.startswith('prebundle')
        fixture=(PREPARATION if prebundle else FINAL).replace("(root/'ready').touch()",MARKER)+CONFIG
        api=IsolatedServer('recovery-budget-'+mode,app='budget_recovery:app',database_url=url)
        (api.root/'budget_recovery.py').write_text(fixture,encoding='utf8')
        (api.root/'mode').write_text('partial_copy' if prebundle else 'prepared')
        readers=[]
        with api:
            api.create_case('Recovery budget '+mode)
            api.put('E-report/report.md','# Recover with original allowance\n\nReal report.\n')
            created=api.call('POST',api.prefix+'/report:build',{});job_id=created['job_id']
            wait_file(api.root/'ready',seconds=90)
            before=api.call('GET',f'/fixture/finalizing/{job_id}')
            proofs=[e['payload'] for e in before['events'] if e['type']=='attempt.output_drained']
            assert len(proofs)==1 and proofs[0]['usage']['observed_output_bytes']>0
            usage=proofs[0]['usage']
            deadline=json.loads((api.root/'budget-deadline.json').read_text())
            if mode in {'missing_usage','duplicate_usage','corrupt_usage','wrong_binding'}:
                api.call('POST',f'/fixture/output-proof/{job_id}',{'mode':mode})
            api.proc.terminate();api.proc.wait(timeout=10)
            if mode.endswith('_expired'):
                expires=deadline['at']+deadline['remaining']
                while time.time()<=expires+1:time.sleep(.2)
            readers=[peer(api) for _ in range(2 if mode=='two_peers' else 1)]
            if mode in {'verification_expires','prebundle_copy_expires'}:
                for reader in readers:
                    path=reader.root/'finalizing_peer.py'
                    with path.open('a',encoding='utf8') as stream:
                        stream.write(EXPIRE_DURING_WORK.replace('COPY_STAGE',repr(prebundle)))
            try:
                with ThreadPoolExecutor(max_workers=len(readers)) as pool:
                    list(pool.map(lambda r:r.__enter__(),readers))
                reader=readers[0]
                invalid=mode in {'missing_usage','duplicate_usage','corrupt_usage','wrong_binding'}
                until=time.monotonic()+70
                while True:
                    after=reader.call('GET',f'/fixture/finalizing/{job_id}')
                    row=reader.call('GET',f'/jobs/{job_id}')
                    if (after['status']!='running' and not row.get('completion_pending')) or (invalid and row.get('recovery')):
                        # The job query follows the event snapshot. Its completed
                        # state can be newer; capture events after that barrier.
                        after=reader.call('GET',f'/fixture/finalizing/{job_id}')
                        break
                    if time.monotonic()>until:raise TimeoutError(str(after))
                    time.sleep(.1)
                result={'mode':mode,'before':before,'after':after,'job':row}
                (api.root/'recovery-budget-observation.json').write_text(json.dumps(result,indent=2),encoding='utf8')
                assert after['attempt_id']==before['attempt_id'] and after['attempt_count']==1
                assert after['publication_id']==before['publication_id']
                usages=[e['payload'] for e in after['events'] if e['type']=='attempt.resource_usage']
                if invalid:
                    assert after['status']=='running' and after['publication_status']=='prepared' and not usages
                else:
                    expected='failed' if mode.endswith(('_expired','_expires')) else 'succeeded'
                    assert after['status']==expected and len(usages)==1
                    assert usages[0]['by_source']==usage['by_source']
                    assert usages[0]['observed_output_bytes']==usage['observed_output_bytes']
                    assert usages[0]['max_log_bytes']==65536
                    if expected=='failed':
                        assert row['attempt']['reason_code']=='resource_limit'
                        assert after['publication_status']=='failed' and usages[0]['violation']=='timeout'
                        if mode=='prebundle_expired':
                            expired=[e['payload'] for e in after['events'] if e['type']=='attempt.bundle_preparation_expired']
                            assert len(expired)==1 and expired[0]['copy_resumed'] is False and expired[0]['storage_reclaimed'] is False
                            if after['checkpoint']:require_dead_owner_drain(after)
                        if mode.endswith('_expires'):
                            assert (reader.root/'finalizing_peer.budget-drained').exists()
                    else:
                        assert after['publication_status']=='committed' and usages[0]['violation'] is None
                assert all(not (r.root/'finalizing_peer.replayed').exists() for r in readers)
            finally:
                for reader in readers:
                    if reader.proc is not None:reader.__exit__(None,None,None)
            with peer(api) as repeated:
                again=repeated.call('GET',f'/fixture/finalizing/{job_id}')
                (api.root/'repeated-budget-observation.json').write_text(json.dumps(again,indent=2),encoding='utf8')
                if mode=='prebundle_expired':
                    require_dead_owner_drain(again)
                    assert without_later_drain(again)==without_later_drain(after),'Terminal accounting or lineage changed'
                else:assert again==after,'Repeated recovery changed accounting or lineage'
            if mode=='prebundle_expired':
                with peer(api) as stable:
                    assert stable.call('GET',f'/fixture/finalizing/{job_id}')==again,'Verified drain was repeated'
            checks.append(mode+'_preserves_budget_and_recovery_authority')
            runs.append({'mode':mode,'evidence':str(api.root)})
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'runs':runs}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--postgres-only',action='store_true')
    parser.add_argument('--modes',default=','.join(MODES));args=parser.parse_args();modes=args.modes.split(',')
    assert set(modes)<=set(MODES)
    report=Path(tempfile.mkdtemp(prefix='recovery-budget-evidence-',dir=SERVER.parents[1]/'tmp'))
    assert not args.postgres_only or args.postgres
    results={} if args.postgres_only else {'sqlite':await asyncio.to_thread(verify,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_recoverybudget_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_recoverybudget_[0-9a-f]{32}',name)
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
    print('RECOVERY_BUDGET_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
