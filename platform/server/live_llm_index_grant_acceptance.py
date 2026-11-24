"""Actual PG/S3 outbox delivery while a case writer holds the SQL lock."""
import asyncio
import json
from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
import asyncio, sqlite3
from contextlib import closing
from sqlalchemy import func, select
from fastapi import Depends
from app.db import get_db, SessionLocal
from app.deps import get_current_user
from app.models import DurableEvent, EventOutbox, OwnerStorageReservation
from app.routers.workspaces import get_owned_case
from app.services import durable_events, indexsvc, quota

async def manual_delivery(factory,stop,**kwargs):
    await stop.wait()
durable_events.dispatch_forever=manual_delivery

@app.post('/api/v1/cases/{case_id}/testing/index-delivery')
async def checked_delivery(case_id:int, enabled:bool, user=Depends(get_current_user), db=Depends(get_db)):
    case=await get_owned_case(case_id,user,db)
    assert db.get_bind().dialect.name=='postgresql' and indexsvc.enabled(case_id)==enabled
    target='Ref/enabled.txt' if enabled else 'Ref/disabled.txt'
    rows=await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case_id,
                         DurableEvent.type=='files.changed').order_by(DurableEvent.seq.desc()))
    event=next(e for e in rows if target in json.loads(e.payload_json)['paths'])
    identifier=event.id
    async def outbox():
        row=await db.scalar(select(EventOutbox).where(EventOutbox.event_id==identifier)
                            .execution_options(populate_existing=True))
        return row.status,row.last_error,row.attempts
    def grants(active_only):
        query=select(func.count()).select_from(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user.id)
        return query.where(OwnerStorageReservation.state=='active') if active_only else query
    def receipts():
        with closing(sqlite3.connect(indexsvc._db_path(case_id))) as index:
            return index.execute('SELECT count(*) FROM consumed_events WHERE event_id=?',(identifier,)).fetchone()[0]
    assert (await outbox())[0]=='pending'
    before=await db.scalar(grants(False))
    async with SessionLocal() as writer:
        await durable_events.lock_case_execution(writer,case_id)
        await durable_events.deliver_pending(SessionLocal)
        status,reason,attempts=await outbox()
        after=await db.scalar(grants(False))
        assert await db.scalar(grants(True))==0
        if enabled:
            assert (status,reason)==('pending','CaseWriteConflict') and receipts()==0,(status,reason)
        else:
            assert status=='delivered' and before==after and not indexsvc.enabled(case_id)
        await quota.check_storage_quota(writer,user,4096)
        await writer.commit()
    if enabled:
        deadline=time.monotonic()+15
        while (await outbox())[0]!='delivered':
            if time.monotonic()>=deadline:raise TimeoutError('Index delivery did not resume')
            await asyncio.sleep(.2)
            await durable_events.deliver_pending(SessionLocal)
        assert receipts()==1
        await durable_events.publish_to_local_bus(event,SessionLocal)
        assert receipts()==1 and (await outbox())[2]>attempts
    return {'index_enabled':enabled,'event':identifier,'during_lock':status,'deferral':reason,
            'reserved_during_delivery':after-before,'active_after_delivery':0,'writer_quota_checked':True,
            'final_outbox':(await outbox())[0],'index_receipts':receipts() if enabled else 0}
'''


def scenario(api, *, store=None, **_):
    assert store is not None and api.database_url
    case=api.create_case('Index delivery and solver admission')
    rows=[]
    for enabled in (False,True):
        if enabled:
            assert api.call('POST',api.prefix+'/index:rebuild',{})['ok']
        name='enabled' if enabled else 'disabled'
        put(api,'Ref/'+name+'.txt','indexgrant'+name)
        rows.append(api.call('POST',api.prefix+'/testing/index-delivery?enabled='+str(enabled).lower(),{}))
    search=api.call('GET',api.prefix+'/search?q=indexgrantenabled')
    assert any(hit['path']=='Ref/enabled.txt' for hit in search['hits'])
    result={'passed':True,'case_id':case['id'],'checks':rows}
    (api.root/'index-grant-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    api.create_case('Index evidence readback')
    return result


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
