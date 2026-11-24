"""Fence abandoned image cleanup from actual births, preserving stop journals."""
import json
import uuid

from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import DurableEvent, NativeRequest
from . import native_request_records as records, sandbox_retirement as retirement
from . import sandbox_recovery_inspection as inspection
from .async_ops import finish_before_cancel
from .durable_events import append_event, lock_case_execution, now

SCHEMA = 'vibeflow.sandbox-image-reconciliation.v1'
EVENT = 'terminal.image_cleanup_abandoned'
FIELDS = ('phase', 'version', 'error', 'ended_at')


def event_id(request_id):
    return str(uuid.uuid5(uuid.UUID(request_id), SCHEMA))


async def identities(db,row):
    from . import sandbox_image_scope as scopes, native_scope_controller as protocol
    await scopes.require_closed(db,row)
    selected = inspection.bounded(row.profile_json)
    inspection.bounded(row.children_json)
    journal = records.document(row)
    if row.kind != 'terminal.cleanup' or len(journal['children']) > 1:
        raise ValueError('Reconciliation requires one registered image cleanup')
    expected = [('cleanup_owner', None, inspection.bounded(row.owner_identity_json))]
    launches={}
    for child in journal['children'].values():
        process = child['identity']
        scope = await scopes.evidence(db,row,child)
        if process is None:
            from .sandbox_image_launch import incomplete_births
            births,digest=await incomplete_births(db,row,child)
            expected.extend(births);launches[child['id']]=protocol.digest({'launch':digest,'scope':scope}) if scope is not None else digest
            continue
        if process['native'] is None:raise ValueError('Cleanup native process identity is missing')
        if process['native']['distribution'] != selected['distro']:
            raise ValueError('Cleanup distribution changed')
        expected.extend([
            ('cleanup_host:' + child['id'], None, process['host']),
            ('cleanup_native:' + child['id'], selected['distro'], process['native']['process']),
        ])
        if scope is not None:
            from .sandbox_image_launch import evidence
            launches[child['id']]=protocol.digest({'launch':await evidence(db,row,child),'scope':scope})
    return expected,launches


def stopped(row):
    journal = records.document(row)
    return row.phase in {'succeeded', 'failed', 'cancelled'} and all(
        child['stop'] is not None and child['stop']['verified'] is True
        for child in journal['children'].values())


def owner_fence(original):
    # _current() rejects registration and release for terminal requests. A live
    # API can therefore coexist with a fully stopped, failed preparation; its
    # unrelated requests must not require restarting the entire service.
    return ('terminal_request' if original['phase'] in {'failed','cancelled'}
            and original['ended_at'] is not None else 'owner_exited')


def require_observations(checks, expected, fence):
    if len(checks)!=len(expected) or fence not in {'owner_exited','terminal_request'}:
        raise ValueError('Incomplete cleanup exit observations')
    for index,(check,(role,distro,birth)) in enumerate(zip(checks,expected)):
        states={'exited','match'} if index==0 and fence=='terminal_request' else {'exited'}
        if (check['role']!=role or check['distribution']!=distro or check['birth']!=birth
                or check['observation']['role']!=role or check['observation']['state'] not in states):
            raise ValueError('Cleanup exit observation does not match its ownership fence')


async def terminal_drain_observed(db,row):
    """Read-only exit check for releasing this caller's retained node claim."""
    if owner_fence({'phase':row.phase,'ended_at':row.ended_at})!='terminal_request':
        return False
    before=retirement.binding(row)
    expected,_=await identities(db,row)
    observed=await inspection.observe(inspection.Snapshot(json.loads(row.profile_json)['session_id'],before,
        ((expected[0][0],expected[0][2],False),),tuple(expected[1:])))
    if observed['status']!='processes_exited' or len(observed['checks'])!=len(expected):
        return False
    observations=[{'role':role,'distribution':distro,'birth':birth,'observation':check}
        for (role,distro,birth),check in zip(expected,observed['checks'])]
    require_observations(observations,expected,'terminal_request')
    current=await db.get(NativeRequest,row.id,populate_existing=True)
    # A concurrent explicit retry can commit its independently verified fence
    # while these probes run. Both states forbid future release by this request.
    return current is not None and (retirement.binding(current)==before or await verified(db,current) is not None)


async def verified(db, row):
    if row.kind != 'terminal.cleanup':
        return None
    event = await db.get(DurableEvent, event_id(row.id), populate_existing=True)
    if event is None:
        return None
    from . import sandbox_image_cleanup as cleanup
    document = inspection.bounded(event.payload_json)
    selected = inspection.bounded(row.profile_json)
    if (event.type != EVENT or event.case_id != row.case_id
            or document['schema'] != SCHEMA or document['request_id'] != row.id
            or document['case_id'] != row.case_id or document['created_by'] != row.created_by
            or document['session_id'] != selected['session_id']
            or document['intent_id'] != selected['intent_id']
            or selected['intent_id'] != cleanup.event_id(selected['session_id'], 'intent')
            or row.phase not in {'failed', 'cancelled'}
            or row.retirement_event_id is not None
            or set(document['original']) != set(FIELDS)
            or document['after_sha256'] != retirement.binding(row)
            or document['before_sha256'] != retirement.binding(row, document['original'])
            or row.version != document['original']['version'] + 1
            or document['normal_stop_receipt_invented'] is not False):
        raise ValueError('Image cleanup reconciliation binding changed')
    intent = await db.get(DurableEvent, selected['intent_id'], populate_existing=True)
    if intent is None or intent.type != cleanup.INTENT or intent.case_id != row.case_id:
        raise ValueError('Reconciliation has no committed cleanup intent')
    committed = inspection.bounded(intent.payload_json)
    if (committed['created_by'] != row.created_by or committed['case_id'] != row.case_id
            or any(selected[key] != committed[key] for key in
                   ('session_id', 'recovery_id', 'image_root', 'disk', 'image_sha256', 'distro'))):
        raise ValueError('Reconciled cleanup differs from committed intent')
    expected,launches = await identities(db,row)
    if document.get('launch_sha256',{})!=launches:
        raise ValueError('Reconciled startup observations changed')
    fence=document.get('owner_fence','owner_exited')
    if fence=='terminal_request' and owner_fence(document['original'])!=fence:
        raise ValueError('A live owner requires an already terminal request fence')
    require_observations(document['observations'],expected,fence)
    return document


async def reconcile(sid, recovery_id, user_id):
    """Explicit cleanup retry only; GET never fences or launches anything."""
    from . import sandbox_image_cleanup as cleanup
    from .case_validation import assert_writable_locked
    # Native revocation can take time. Authorize the exact image first, then
    # perform it outside the business transaction before collecting exit proofs.
    async with SessionLocal() as db:
        original, case, witness, snapshot = await cleanup.evidence(db,sid,user_id)
        if witness['recovery_id'] != recovery_id:
            raise ValueError('Cleanup must retain the retirement snapshot')
        requests=list(await db.scalars(select(NativeRequest).where(
            NativeRequest.case_id==case.id,NativeRequest.kind=='terminal.cleanup').limit(129)))
        if len(requests)>128:raise ValueError('Cleanup history exceeds reconciliation bound')
        recover=[]
        for request in requests:
            selected=inspection.bounded(request.profile_json)
            if selected.get('session_id')!=sid and selected.get('image_root')!=snapshot['image_root']:continue
            if request.created_by!=user_id or selected.get('session_id')!=sid or selected.get('image_root')!=snapshot['image_root']:
                raise ValueError('Cleanup request binding changed')
            recover.append(request.id)
    from .sandbox_image_scope import recover as recover_scope
    for identifier in recover:await recover_scope(identifier,user_id)
    async with SessionLocal() as db:
        row, case, witness, snapshot = await cleanup.evidence(db, sid, user_id)
        if witness['recovery_id'] != recovery_id:
            raise ValueError('Cleanup must retain the retirement snapshot')
        intent = await cleanup.committed(db, row, witness, snapshot)
        if intent is None:
            return
        case_id = case.id
        rows = list(await db.scalars(select(NativeRequest).where(
            NativeRequest.case_id == case_id, NativeRequest.kind == 'terminal.cleanup'
        ).order_by(NativeRequest.id).limit(129)))
        if len(rows) > 128:
            raise ValueError('Cleanup history exceeds reconciliation bound')
        pending = []
        for request in rows:
            selected = inspection.bounded(request.profile_json)
            if not selected.get('session_id') or not selected.get('image_root'):
                raise ValueError('Unbound cleanup request')
            if selected['session_id'] != sid and selected['image_root'] != snapshot['image_root']:
                continue
            if (request.created_by != user_id or selected['session_id'] != sid
                    or selected['image_root'] != snapshot['image_root']):
                raise ValueError('Cleanup request binding changed')
            if await verified(db, request) is not None or stopped(request):
                continue
            expected,launches = await identities(db,request)
            fence=owner_fence({'phase':request.phase,'ended_at':request.ended_at})
            pending.append((request.id, retirement.binding(request), expected,launches,fence))
    for request_id, before, expected, launches, fence in pending:
        # Potentially slow OS probes stay outside the case transaction. An owner
        # of an active request must exit; a terminal request cannot register or
        # release more work, but every known child still must actually exit.
        observed = await inspection.observe(inspection.Snapshot(sid, before,
            ((expected[0][0], expected[0][2], fence=='owner_exited'),), tuple(expected[1:])))
        if observed['status'] != 'processes_exited':
            raise inspection.InspectionBlocked(observed['reason'])
        checks = observed['checks']
        if len(checks)!=len(expected):raise ValueError('Incomplete cleanup exit observations')
        observations=[{'role':role,'distribution':distro,'birth':birth,'observation':check}
            for (role,distro,birth),check in zip(expected,checks)]
        require_observations(observations,expected,fence)
        async with SessionLocal() as db:
            await lock_case_execution(db, case_id)
            await assert_writable_locked(db, case_id)
            row, _, witness, snapshot = await cleanup.evidence(db, sid, user_id)
            if await cleanup.committed(db, row, witness, snapshot) != intent:
                raise ValueError('Cleanup intent changed during inspection')
            request = await db.get(NativeRequest, request_id, populate_existing=True)
            if request is None:
                raise ValueError('Cleanup request disappeared')
            if await verified(db, request) is not None:
                continue
            if retirement.binding(request) != before:
                raise ValueError('Cleanup request changed during inspection')
            if await identities(db,request)!=(expected,launches):
                raise ValueError('Cleanup startup changed during inspection')
            after = {'phase': 'cancelled' if request.phase == 'cancelled' else 'failed',
                'version': request.version + 1, 'error': request.error or 'OwnerExitReconciled',
                'ended_at': request.ended_at or now()}
            document = {'schema': SCHEMA, 'request_id': request_id, 'case_id': case_id,
                'created_by': user_id, 'session_id': sid, 'intent_id': cleanup.event_id(sid, 'intent'),
                'observed_at': observed['observed_at'], 'normal_stop_receipt_invented': False,
                'original': {key: retirement.stamp(getattr(request, key)) for key in FIELDS},
                'before_sha256': before, 'after_sha256': retirement.binding(request, after),
                'launch_sha256':launches,
                'owner_fence':fence,'observations':observations}
            await append_event(db, case_id, EVENT, document, event_id=event_id(request_id))
            changed = await db.execute(update(NativeRequest).where(NativeRequest.id == request_id,
                NativeRequest.version == request.version).values(**after).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise ValueError('Cleanup ownership changed during reconciliation')
            request = await db.get(NativeRequest, request_id, populate_existing=True)
            await verified(db, request)
            await finish_before_cancel(db.commit())
