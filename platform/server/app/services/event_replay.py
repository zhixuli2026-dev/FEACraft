"""Read committed case events in sequence, independent of outbox delivery order."""
import json

from sqlalchemy import func, select

from ..models import DurableEvent, JobAttempt

MAX_CURSOR = 2**53 - 1  # Exact in browser Number as well as the database.


async def latest_sequence(db, case_id):
    return await db.scalar(select(func.max(DurableEvent.seq)).where(DurableEvent.case_id==case_id)) or 0


def envelope(event):
    payload = json.loads(event.payload_json)
    return {**payload, 'schema':'vibeflow.workflow-event.v1', 'event_id':event.id,
        'case_id':event.case_id, 'seq':event.seq, 'type':event.type,
        'run_id':event.run_id, 'step_id':event.step_id, 'attempt_id':event.attempt_id,
        'payload':payload, 'created_at':event.created_at.isoformat()}


async def visible(db, event):
    """A committed success still waits for compatibility publication to settle."""
    if event.type != 'jobs.updated':return True
    payload = json.loads(event.payload_json)
    if payload.get('status') != 'succeeded' or not event.attempt_id:return True
    from .job_execution import completion_visibility
    attempt = await db.get(JobAttempt, event.attempt_id, populate_existing=True)
    return (attempt is not None and attempt.job_id == payload.get('job_id')
        and completion_visibility(attempt) not in {'pending','invalid'})


async def page(db, case_id, after, limit=100):
    if type(after) is not int or not 0 <= after <= MAX_CURSOR or not 1 <= limit <= 200:
        raise ValueError('Invalid event cursor or page size')
    watermark = await latest_sequence(db, case_id)
    if after > watermark:
        raise ValueError('Event cursor is ahead of this case; reconcile current state and restart at zero')
    rows = list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case_id,
        DurableEvent.seq > after, DurableEvent.seq <= watermark).order_by(DurableEvent.seq).limit(limit+1)))
    events, cursor, blocked = [], after, False
    for row in rows[:limit]:
        if not await visible(db,row):
            blocked = True
            break
        events.append(envelope(row)); cursor = row.seq
    return {'case_id':case_id, 'events':events, 'next_after':cursor, 'watermark':watermark,
        'has_more':not blocked and len(rows)>limit, 'blocked':blocked}
