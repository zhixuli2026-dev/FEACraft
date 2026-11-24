"""Durable cgroup allocation and revocation for a single cleanup child."""
import copy
import json
import uuid
from pathlib import Path

from ..db import SessionLocal
from ..models import DurableEvent, NativeRequest
from . import native_request_records as records, native_scope_controller as protocol
from . import native_scope_control as control, sandbox_image_launch as launch
from .durable_events import append_event, json_text
from .async_ops import finish_before_cancel

SCHEMA = 'vibeflow.image-native-scope.v1'
ADMISSION_SCHEMA = 'vibeflow.image-native-scope.v2'
GUARDIAN_SCHEMA = 'vibeflow.image-native-scope.v3'
CURRENT_SCHEMA = 'vibeflow.image-native-scope.v4'
STAGES = ('planned', 'prepared', 'allocated', 'admitted', 'closed')


def history(value):
    return {key: item for key, item in value.items() if key != 'closed'}


def event_id(child_id, stage):
    return str(uuid.uuid5(uuid.UUID(child_id), SCHEMA + ':' + stage))


async def evidence(db, row, child):
    values = {}
    schemas = set()
    binding = launch.binding(row, child)
    for stage in STAGES:
        event = await db.get(DurableEvent, event_id(child['id'], stage), populate_existing=True)
        if event is None:
            values[stage] = None
            continue
        if event.case_id != row.case_id or event.type != 'terminal.image_scope_' + stage or len(event.payload_json.encode()) > 65536:
            raise ValueError('Invalid image scope event')
        value = json.loads(event.payload_json)
        if set(value) != {'schema', 'binding', 'value'} or value['schema'] not in {SCHEMA, ADMISSION_SCHEMA, GUARDIAN_SCHEMA, CURRENT_SCHEMA} or value['binding'] != binding:
            raise ValueError('Image scope event differs from its child')
        schemas.add(value['schema'])
        values[stage] = value['value']
    if len(schemas) > 1:
        raise ValueError('Image scope event versions differ')
    if schemas == {SCHEMA}:
        if values['admitted'] is not None:
            raise ValueError('Legacy scope cannot claim a new admission boundary')
        del values['admitted']  # Preserve the exact historical reconciliation digest.
    planned, prepared, allocated, closed = (values[key] for key in ('planned','prepared','allocated','closed'))
    intent, observations = await launch.evidence(db, row, child)
    if planned is None:
        if any(item is not None for key,item in values.items() if key != 'planned') or intent and 'scope_plan_sha256' in intent:
            raise ValueError('Image scope lacks its original plan')
        return None
    protocol.validate(planned, check_code=False)
    if planned['operation'] != 'allocate' or planned['distribution'] != binding['distribution']:
        raise ValueError('Image allocation changed its operation or distribution')
    if prepared is not None:
        control.validate_prepared(prepared)
        if prepared['request'] != planned or intent is None or intent.get('scope_plan_sha256') != protocol.digest(planned):
            raise ValueError('Image allocation preparation lacks its exact intent')
        if intent['launch_nonce'] != prepared['native']['reservation']['nonce'] or intent['native_scope_helpers'] != planned['helpers']:
            raise ValueError('Image launch differs from its prepared allocation')
    elif intent is not None:
        raise ValueError('Image intent committed without its allocation preparation')
    if allocated is not None:
        control.validate_receipt(allocated)
        if allocated['request'] != planned or allocated['preparation'] != prepared:
            raise ValueError('Image allocation receipt changed its prepared creator')
    if 'admitted' in values:
        admitted = values['admitted']
        if admitted is not None:
            expected = {'allocated_sha256': protocol.digest(allocated)}
            if schemas in ({GUARDIAN_SCHEMA},{CURRENT_SCHEMA}):
                from .sandbox_image_guardian import selection
                expected['guardian'] = selection(row, child, intent, admitted.get('guardian'))
            if schemas == {CURRENT_SCHEMA}:
                from .sandbox_image_guardian import native_selection
                expected['native_preflight']=native_selection(admitted.get('native_preflight'),intent,planned['distribution'])
                if child['identity'] is not None and (child['identity']['host']!=expected['native_preflight']['host']
                        or child['identity']['native']!=expected['native_preflight']['native']):
                    raise ValueError('Registered business identity differs from admitted native preflight')
                for observation in observations:
                    if (observation['host']!=expected['native_preflight']['host'] or observation['native'] is not None
                            and observation['native']!=expected['native_preflight']['native']):
                        raise ValueError('Business startup differs from its admitted native preflight')
            if allocated is None or admitted != expected:
                raise ValueError('Image admission is not bound to the committed allocation')
        if admitted is None and (observations or child['identity'] is not None):
            raise ValueError('Image business process exists without committed admission')
    if closed is not None:
        fields = {'allocation_sha256', 'receipt'} | ({'guardian'} if schemas in ({GUARDIAN_SCHEMA},{CURRENT_SCHEMA}) else set())
        if set(closed) != fields or closed['allocation_sha256'] != protocol.digest(history(values)):
            raise ValueError('Image revocation differs from its allocation history')
        if schemas in ({GUARDIAN_SCHEMA},{CURRENT_SCHEMA}):
            if values['admitted'] is None:
                if closed['guardian'] is not None:
                    raise ValueError('Unadmitted business cannot claim a durable guardian selection')
            else:
                from .sandbox_image_guardian import validate_proof
                validate_proof(values['admitted']['guardian'], closed['guardian'])
        receipt = closed['receipt']
        if prepared is None:
            if receipt is not None:
                raise ValueError('Unpermitted image allocation cannot have a fabricated native receipt')
        else:
            control.validate_receipt(receipt)
            request = receipt['request']
            expected = ({'scope': allocated['result']} if allocated is not None else
                        {'allocation_request': planned, 'preparation': prepared['native']})
            if (request['operation'] != ('retire' if allocated is not None else 'abandon')
                    or request['distribution'] != planned['distribution'] or request['arguments'] != expected):
                raise ValueError('Image revocation selected another allocation')
    return values


async def append(db, row, child, stage, value):
    if await db.get(DurableEvent, event_id(child['id'], stage)) is not None:
        raise ValueError('Image scope stage already committed')
    schema = CURRENT_SCHEMA
    if stage != 'planned':
        original = await db.get(DurableEvent, event_id(child['id'],'planned'))
        if original is None:
            raise ValueError('Image scope has no committed original schema')
        schema = json.loads(original.payload_json)['schema']
    await append_event(db, row.case_id, 'terminal.image_scope_' + stage,
        {'schema': schema, 'binding': launch.binding(row, child), 'value': value}, event_id=event_id(child['id'], stage))


async def current(db, owner, child_id, *, launching):
    from .case_validation import admit_session_locked
    if launching:
        await admit_session_locked(db, NativeRequest, owner.id)
    row = await records._current(db, owner, owner.nonce, launch=launching)
    child = records.document(row)['children'][child_id]
    if child['phase'] != 'preparing' and launching:
        raise ValueError('Image child is no longer preparing')
    return row, child


async def plan(owner, child_id, request):
    protocol.validate(request)
    async with SessionLocal() as db:
        row, child = await current(db, owner, child_id, launching=True)
        await launch.require_unsealed(db,child)
        if await evidence(db, row, child) is not None:
            raise ValueError('Image child already selected an allocation controller')
        await append(db, row, child, 'planned', request)
        await db.commit()


async def prepared(db, row, child, value):
    control.validate_prepared(value)
    previous = await evidence(db, row, child)
    if previous is None or previous['planned'] != value['request'] or any(item is not None for key,item in previous.items() if key != 'planned'):
        raise ValueError('Image allocation preparation changed or is already closed')
    await append(db, row, child, 'prepared', value)


async def allocated(owner, child_id, receipt):
    control.validate_receipt(receipt)
    async with SessionLocal() as db:
        row, child = await current(db, owner, child_id, launching=True)
        previous = await evidence(db, row, child)
        if (previous is None or previous['prepared'] != receipt.get('preparation')
                or previous['allocated'] is not None or previous['closed'] is not None):
            raise ValueError('Image allocation receipt cannot be committed')
        await append(db, row, child, 'allocated', receipt)
        await db.commit()


async def require_allocated(db, row, child):
    value = await evidence(db, row, child)
    if value is None:
        return None  # Historical launches retain their existing stricter rules.
    if value['allocated'] is None or value['closed'] is not None:
        raise ValueError('Image native scope is not available for admission')
    if 'admitted' in value and value['admitted'] is None:
        raise ValueError('Image business launch has not been durably admitted')
    return value['allocated']['result']


async def admit(owner, child_id, *, guardian, native_preflight):
    """Bind both actual bootstrap births before the native gate may join/run."""
    async with SessionLocal() as db:
        row, child = await current(db, owner, child_id, launching=True)
        value = await evidence(db,row,child)
        if (value is None or 'admitted' not in value or value['allocated'] is None
                or value['admitted'] is not None or value['closed'] is not None):
            raise ValueError('Image scope cannot admit another business launch')
        from .sandbox_image_guardian import selection,native_selection
        intent, _ = await launch.evidence(db,row,child)
        selection(row,child,intent,guardian)
        native_selection(native_preflight,intent,value['planned']['distribution'])
        await append(db,row,child,'admitted',{'allocated_sha256':protocol.digest(value['allocated']),
            'guardian':guardian,'native_preflight':native_preflight})
        await db.commit()


async def unadmitted_births(db,row,child):
    """A missing permit means no business execution was admitted.

    This is not an exit observation for a process whose birth is unknown. Old
    schemas lack this negative evidence and retain their previous refusal.
    In v4 fixed read-only bootstrap processes may already exist, but cannot
    enter the selected cgroup or run the business gate without a birth permit.
    """
    value = await evidence(db,row,child)
    if value is None or 'admitted' not in value or value['admitted'] is not None or value['closed'] is None:
        return None
    births = []
    prepared = value['prepared']
    if prepared is not None:
        births = [('allocation_host:'+child['id'],None,prepared['host']['host']),
                  ('allocation_creator:'+child['id'],value['planned']['distribution'],prepared['native']['controller'])]
    return births,protocol.digest({'business_not_admitted':True,'scope':value})


async def guarded_births(db,row,child):
    """v4 has durable bootstrap births; v3 only proves the pre-first-release gap."""
    value = await evidence(db,row,child)
    if (value is None or value.get('admitted') is None or 'guardian' not in value['admitted']
            or value['closed'] is None or value['closed'].get('guardian') is None):
        return None
    intent, observations = await launch.evidence(db,row,child)
    preflight=value['admitted'].get('native_preflight')
    if preflight is not None:
        prepared=value['prepared']
        births=[('allocation_host:'+child['id'],None,prepared['host']['host']),
                ('allocation_creator:'+child['id'],value['planned']['distribution'],prepared['native']['controller']),
                ('cleanup_guardian:'+child['id'],None,value['admitted']['guardian']['preparation']['guardian']),
                ('cleanup_host:'+child['id'],None,preflight['host']),
                ('cleanup_native:'+child['id'],preflight['native']['distribution'],preflight['native']['process'])]
        return births,protocol.digest({'same_process_native_preflight':True,'scope':value,
                                       'intent':intent,'observations':observations})
    if observations:
        return None  # host_ready without native birth is still a distinct gap.
    prepared = value['prepared']
    return [('allocation_host:'+child['id'],None,prepared['host']['host']),
            ('allocation_creator:'+child['id'],value['planned']['distribution'],prepared['native']['controller']),
            ('cleanup_guardian:'+child['id'],None,value['admitted']['guardian']['preparation']['guardian'])], protocol.digest(
                {'windows_scope_exited_before_first_release':True,'scope':value})


async def require_closed(db, row):
    if row.kind != 'terminal.cleanup':
        return
    for child in records.document(row)['children'].values():
        value = await evidence(db, row, child)
        if value is not None and value['closed'] is None:
            raise ValueError('Image native scope has no verified revocation')


async def closure(db,row,child,value,receipt,guardian_proof):
    closed = {'allocation_sha256': protocol.digest(history(value)), 'receipt': receipt}
    original = await db.get(DurableEvent,event_id(child['id'],'planned'))
    if json.loads(original.payload_json)['schema'] in {GUARDIAN_SCHEMA,CURRENT_SCHEMA}:
        closed['guardian'] = guardian_proof if value['admitted'] is not None else None
        if value['admitted'] is not None:
            from .sandbox_image_guardian import validate_proof
            validate_proof(value['admitted']['guardian'],guardian_proof)
    return closed


async def close(owner, child_id, *, guardian_proof=None):
    """Called after the sole control operation and business execution drain."""
    async with SessionLocal() as db:
        row, child = await current(db, owner, child_id, launching=False)
        value = await evidence(db, row, child)
        if value is None or value['closed'] is not None:
            return
    receipt = None
    if value['prepared'] is not None:
        operation = 'retire' if value['allocated'] is not None else 'abandon'
        arguments = ({'scope': value['allocated']['result']} if operation == 'retire' else
                     {'allocation_request': value['planned'], 'preparation': value['prepared']['native']})
        receipt = await control.control(value['planned']['distribution'], operation, arguments)
    async with SessionLocal() as db:
        row, child = await current(db, owner, child_id, launching=False)
        if await evidence(db, row, child) != value:
            raise ValueError('Image allocation changed while revoking')
        await append(db, row, child, 'closed', await closure(db,row,child,value,receipt,guardian_proof))
        await db.commit()


async def prepare(owner, child_id, process, distro, *, check_active=None):
    def active():
        if check_active is not None:check_active()
    async def commit(value):
        if process.proc is not None or process.record is not None:
            raise ValueError('Image payload started before scope allocation')
        process.nonce = value['native']['reservation']['nonce']
        await launch.reserve(owner, child_id, process, scope_preparation=value)
        active()
    controller = control.ScopeControl(distro, 'allocate', {'seed': process.nonce}, on_prepared=commit, check_active=active)
    await plan(owner, child_id, copy.deepcopy(controller.request))
    active()
    receipt = await controller.run()
    await finish_before_cancel(allocated(owner, child_id, receipt))
    # Allocation may finish while cancellation drains. Keep its receipt before
    # stopping so cleanup can revoke that exact allocation, without launching.
    active()
    process.native_scope = {'scope': copy.deepcopy(receipt['result']),
                            'helper_sha256': receipt['request']['helpers']['process_cgroup_native.py']}
    async with SessionLocal() as db:
        row, child = await current(db,owner,child_id,launching=True)
        root = Path(json.loads(row.profile_json)['root']) / ('windows-' + child_id)
    active()
    await process.prepare_guardian(root)
    async def admitted(native_preflight):
        await admit(owner,child_id,guardian={'request':copy.deepcopy(process.guardian.request),
            'preparation':copy.deepcopy(process.guardian.prepared)},native_preflight=native_preflight)
        active()
    process.on_native_preflight=admitted


async def recover(request_id, user_id):
    """Explicit retry may revoke only after a terminal fence or actual owner exit."""
    from . import sandbox_retirement as retirement, process_identity_native as identity
    from .durable_events import lock_case_execution
    from .case_validation import assert_writable_locked
    async with SessionLocal() as db:
        row = await db.get(NativeRequest, request_id)
        if row is None or row.kind != 'terminal.cleanup' or row.created_by != user_id:
            raise ValueError('Invalid image scope recovery owner')
        children = list(records.document(row)['children'].values())
        if not children:
            return
        if len(children) != 1:
            raise ValueError('Image scope recovery requires one child')
        child = children[0]
        value = await evidence(db, row, child)
        if value is None or value['closed'] is not None:
            return
        before = retirement.binding(row)
        case_id = row.case_id
        owner = json.loads(row.owner_identity_json)
        terminal = row.phase in {'failed', 'cancelled'} and row.ended_at is not None
    import asyncio
    observed = await asyncio.to_thread(identity.probe, owner)
    if observed['state'] not in ({'match', 'exited'} if terminal else {'exited'}):
        raise ValueError('Image scope recovery has no owner fence')
    async with SessionLocal() as db:
        await lock_case_execution(db, case_id)
        await assert_writable_locked(db, case_id)
        row = await db.get(NativeRequest, request_id, populate_existing=True)
        if retirement.binding(row) != before or await evidence(db, row, child) != value:
            raise ValueError('Image scope changed before recovery')
    guardian_proof = None
    if value.get('admitted') is not None and 'guardian' in value['admitted']:
        from .sandbox_image_guardian import recover as recover_guardian
        guardian_proof = await recover_guardian(value['admitted']['guardian'])
    receipt = None
    if value['prepared'] is not None:
        operation = 'retire' if value['allocated'] is not None else 'abandon'
        args = ({'scope': value['allocated']['result']} if operation == 'retire' else
                {'allocation_request': value['planned'], 'preparation': value['prepared']['native']})
        receipt = await control.control(value['planned']['distribution'], operation, args)
    async with SessionLocal() as db:
        await lock_case_execution(db, case_id)
        await assert_writable_locked(db, case_id)
        row = await db.get(NativeRequest, request_id, populate_existing=True)
        if retirement.binding(row) != before:
            raise ValueError('Image request changed during scope recovery')
        current_value = await evidence(db, row, child)
        if current_value['closed'] is not None:
            return
        if current_value != value:
            raise ValueError('Image allocation changed during recovery')
        await append(db, row, child, 'closed', await closure(db,row,child,value,receipt,guardian_proof))
        await finish_before_cancel(db.commit())
