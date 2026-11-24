"""Bounded recovery discovery over real legacy records with a concurrent insertion."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main


FIXTURE = """
from contextvars import ContextVar
import asyncio
from sqlalchemy import event, func, insert, select
from fastapi import Depends
from app.db import SessionLocal, engine, get_db
from app.deps import get_current_user
from app.models import Job, JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import job_recovery

tracking = ContextVar('recovery_scan_evidence', default=None)
original_reconcile = job_recovery.reconcile_job
async def observe_reconcile(identifier):
    trace = tracking.get()
    if trace is not None:
        trace['ids'].append(identifier)
        if trace['pause'] and len(trace['ids']) == 1:
            (root / 'page-loaded').touch()
            limit = time.monotonic() + 60
            while not (root / 'resume-scan').exists():
                if time.monotonic() > limit:
                    raise TimeoutError('Concurrent insertion never completed')
                await asyncio.sleep(.05)
    return await original_reconcile(identifier)
job_recovery.reconcile_job = observe_reconcile

@event.listens_for(engine.sync_engine, 'before_cursor_execute')
def observe_query(conn, cursor, statement, parameters, context, many):
    trace = tracking.get()
    if trace is not None and 'select distinct jobs.id' in statement.lower():
        limit = context.compiled.statement._limit_clause
        trace['limits'].append(limit.value if limit is not None else None)

@app.post('/api/v1/testing/recovery-scan/{case_id}/seed')
async def seed_history(case_id:int, body:dict, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    amount = body['count']
    assert amount in (1, job_recovery.RECOVERY_PAGE_SIZE + 3)
    values = [dict(case_id=case_id,type='shell',command='historical command',status='succeeded',exit_code=0)
              for _ in range(amount)]
    ids = (await db.scalars(insert(Job).returning(Job.id),values)).all()
    await db.commit()
    return {'ids':sorted(ids)}

@app.post('/api/v1/testing/recovery-scan/{case_id}/run')
async def run_scan(case_id:int, body:dict, user=Depends(get_current_user), db=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    await db.rollback()
    trace = {'ids':[], 'limits':[], 'pause':body['pause']}
    token = tracking.set(trace)
    try:
        trace['outcomes'] = await job_recovery.reconcile_startup(local_worker=body['local_worker'])
    finally:
        tracking.reset(token)
    trace['attempts'] = await db.scalar(select(func.count(JobAttempt.id)).join(Job, Job.id==JobAttempt.job_id)
                                       .where(Job.case_id==case_id))
    return trace
"""


def scenario(api, *, store=None, **_):
    source = api.prefix
    case_id = api.case_id
    route = '/testing/recovery-scan/'+str(case_id)
    # A pass performs the full production reconciliation of every selected row.
    original_open = api.opener.open
    def open_request(request, *, timeout):
        return original_open(request, timeout=180 if request.selector.endswith('/run') else timeout)
    api.opener.open = open_request
    from app.services.job_recovery import RECOVERY_PAGE_SIZE
    initial = api.call('POST', route+'/seed', {'count':RECOVERY_PAGE_SIZE+3})['ids']
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation = pool.submit(api.call, 'POST', route+'/run', {'pause':True,'local_worker':False})
        try:
            wait_for((api.root/'page-loaded').is_file, 'Initial SQL page was never read')
            inserted = api.call('POST', route+'/seed', {'count':1})['ids']
        finally:
            (api.root/'resume-scan').touch()
        first = operation.result()
    second = api.call('POST',route+'/run',{'pause':False,'local_worker':False})
    local = api.call('POST',route+'/run',{'pause':False,'local_worker':True})
    assert first['ids']==initial and second['ids']==initial+inserted
    assert local['ids']==[] and local['outcomes']=={}
    assert first['limits']==second['limits']==[RECOVERY_PAGE_SIZE]*2 and local['limits']==[RECOVERY_PAGE_SIZE]
    for trace in (first,second,local):
        assert trace['attempts']==0 and sum(trace['outcomes'].values())==len(trace['ids'])
    history = api.call('GET',source+'/jobs')
    assert all(row['status']=='succeeded' and row['attempt'] is None and row['ended_at'] is None for row in history)
    result = {'page_size':RECOVERY_PAGE_SIZE, 'first':first, 'second':second, 'local_worker':local,
              'concurrent_insert':inserted, 'storage':'s3' if store else 'fs',
              'database':'postgresql' if api.database_url else 'sqlite'}
    (api.root/'recovery-pages.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE))
