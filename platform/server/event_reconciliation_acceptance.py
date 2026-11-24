"""Reviewed actual API/browser event reconciliation on isolated native history.

The fixture can stop/restart its own dispatcher and remove one pending delivery
intent for a real files.changed event. It never fabricates a model, file body,
business Job, execution status, or native stop receipt. Lifecycle and source
hash checks are inherited from the reviewed actual-process harness.
"""
import sys
import case_workflow_controls_acceptance as controls

EXTENSION = '''
import asyncio,hashlib
from fastapi import HTTPException
from sqlalchemy import select,delete
from app.models import DurableEvent,EventOutbox

@app.post('/api/v1/test/event-delivery/{mode}')
async def event_delivery(mode:str,user=Depends(get_current_user)):
    if user.id!=1:raise HTTPException(403,'Isolated owner required')
    task=app.state.outbox_task
    if mode=='pause':
        app.state.outbox_stop.set()
        try:await asyncio.wait_for(asyncio.shield(task),5)
        except asyncio.TimeoutError:raise HTTPException(409,{'code':'outbox_draining','message':'Original dispatcher is still draining'})
        return {'paused':task.done(),'original_dispatcher_drained':True}
    if mode=='resume':
        if not task.done():
            if app.state.outbox_stop.is_set():raise HTTPException(409,'Original dispatcher still draining')
            return {'running':True,'already_running':True}
        from app.services.durable_events import dispatch_forever
        app.state.outbox_stop=asyncio.Event()
        app.state.outbox_task=asyncio.create_task(dispatch_forever(SessionLocal,app.state.outbox_stop))
        return {'running':not app.state.outbox_task.done(),'started_after_original_exit':True}
    raise HTTPException(400,'Unknown isolated dispatcher operation')

def event_digest(row):
    return hashlib.sha256(json.dumps([row.id,row.case_id,row.seq,row.type,row.payload_json],ensure_ascii=False).encode()).hexdigest()

@app.post('/api/v1/test/events/{case_id}/{event_id}:lose-outbox')
async def lose_outbox(case_id:int,event_id:str,user=Depends(get_current_user)):
    if user.id!=1:raise HTTPException(403,'Isolated owner required')
    if not app.state.outbox_task.done():raise HTTPException(409,'Pause and drain the actual dispatcher first')
    async with SessionLocal() as db:
        await get_owned_case(case_id,user,db,write=True)
        event=await db.get(DurableEvent,event_id)
        if event is None or event.case_id!=case_id or event.type!='files.changed':raise HTTPException(409,'Expected actual file-commit event')
        row=await db.scalar(select(EventOutbox).where(EventOutbox.event_id==event_id))
        if row is None or row.status!='pending' or row.attempts!=0:raise HTTPException(409,'Expected pending, never-delivered intent')
        marker=Path.cwd()/('outbox-loss-'+event_id+'.json')
        before=event_digest(event)
        with marker.open('x',encoding='utf8') as stream:
            json.dump({'case_id':case_id,'event_id':event_id,'seq':event.seq,'event_sha256':before,'removed_outbox_id':row.id,'fault':'explicit_pending_delivery_intent_deleted','no_business_or_stop_receipt_changes':True},stream,indent=2)
        await db.execute(delete(EventOutbox).where(EventOutbox.id==row.id,EventOutbox.status=='pending'))
        await db.commit()
        return {'event_id':event_id,'seq':event.seq,'event_sha256':before,'outbox_removed':True,'durable_event_retained':True}

@app.get('/api/v1/test/events/{case_id}/{event_id}')
async def inspect_event(case_id:int,event_id:str,user=Depends(get_current_user)):
    if user.id!=1:raise HTTPException(403,'Isolated owner required')
    async with SessionLocal() as db:
        await get_owned_case(case_id,user,db,write=False)
        event=await db.get(DurableEvent,event_id)
        if event is None or event.case_id!=case_id:raise HTTPException(404,'No matching actual event')
        rows=list(await db.scalars(select(EventOutbox).where(EventOutbox.event_id==event_id)))
        return {'event_id':event.id,'seq':event.seq,'type':event.type,'event_sha256':event_digest(event),
            'outbox':[{'id':row.id,'status':row.status,'attempts':row.attempts,'last_error':row.last_error} for row in rows]}
'''
APP = controls.APP + EXTENSION


def run():
    controls.APP = APP
    return controls.run()


if __name__ == '__main__':
    sys.exit(run())
