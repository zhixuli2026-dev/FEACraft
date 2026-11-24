"""Independent Windows/WSL ownership for original-attempt recovery validators.

New evidence is bound to a NativeRequest, never substituted for an original
worker's process-stop record. Historical requests without this evidence retain
their refusal. Kernel retirement and Windows membership receipts are separate.
"""
import asyncio,copy,hashlib,json,math,os,time,uuid
from pathlib import Path

from .. import storage
from ..db import SessionLocal
from ..models import DurableEvent,NativeRequest
from . import native_request_records as records,owned_process,process_preparation
from . import native_scope_control as control,native_scope_controller as protocol
from . import windows_scope_guardian as guardian,process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import append_event,json_text,lock_case_execution

SCHEMA='vibeflow.finalizer-native-scope.v1'
DIRECTORY='.vibeflow-node-finalizers'
STAGES=('plan','prepared','allocated','guardian','admitted',*process_preparation.STAGES,'closed')
HOST_FILES=('owned_process.py','windows_process_create.py','windows_process_scope.py','windows_process_lifetime.py',
    'windows_scope_guard.py','windows_scope_guardian.py','native_process_preflight.py','process_gate.py',
    'process_identity_native.py','process_cgroup_native.py','async_ops.py','native_execution.py',
    'native_request_execution.py','native_request_records.py','finalizing_native_scope.py','finalizing_native_usage.py',
    'finalizing_request_recovery.py','execution_budget.py','execution_output_checkpoint.py','native_diagnostics.py')


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()
def event_id(child_id,stage):return str(uuid.uuid5(uuid.UUID(child_id),SCHEMA+':'+stage))
def fingerprint(row):
    return digest([row.id,row.phase,row.version,row.owner_id,row.owner_identity_json,row.launch_nonce,
        row.profile_json,row.children_json,row.result_json])


def binding(row,child):
    from .job_output_finalizing import native_profile
    if row.kind!='job.finalize':raise ValueError('Independent scope requires a job finalizer')
    profile=native_profile(json.loads(row.profile_json));native=records.is_native(row)
    return {'schema':SCHEMA,'request_id':row.id,'case_id':row.case_id,'created_by':row.created_by,
        'owner_id':row.owner_id,'owner_identity_sha256':hashlib.sha256(row.owner_identity_json.encode()).hexdigest(),
        'profile_sha256':digest(profile),'child_id':child['id'],'parent_launch_nonce':row.launch_nonce,
        'request_sha256':child['request_sha256'],'purpose':child['purpose'],'caller':child['caller'],
        'runtime':'wsl' if native else 'windows','distribution':profile['distro'] if native else None}


def code():
    names=set(HOST_FILES)|set(protocol.FILES)
    return {n:hashlib.sha256(owned_process.GATE.with_name(n).read_bytes()).hexdigest() for n in sorted(names)}


async def read(db,row,child):
    from . import finalizing_linux_scope as linux
    if linux.applies(row):return await linux.read(db,row,child)
    bound=binding(row,child);values={}
    for stage in STAGES:
        event=await db.get(DurableEvent,event_id(child['id'],stage),populate_existing=True)
        if event is None:values[stage]=None;continue
        value=json.loads(event.payload_json)
        if (event.case_id!=row.case_id or event.type!='job.finalizer_scope_'+stage
                or len(event.payload_json.encode())>128*1024 or set(value)!={'binding','value'} or value['binding']!=bound):
            raise ValueError('Independent scope history differs from its request')
        values[stage]=value['value']
    plan=values['plan']
    if plan is None:
        if any(v is not None for v in values.values()):raise ValueError('Independent scope lacks its plan')
        return None
    expected_files=set(HOST_FILES)|set(protocol.FILES)
    if (set(plan)!={'seed','code','allocation'} or not isinstance(plan['seed'],str) or len(plan['seed'])!=64
            or set(plan['code']) not in (expected_files,expected_files-{'native_compute_v1.py'})
            or any(not isinstance(h,str) or len(h)!=64 or any(c not in '0123456789abcdef' for c in h) for h in plan['code'].values())):
        raise ValueError('Invalid independent scope plan')
    native=bound['runtime']=='wsl';prepared=values['prepared'];allocated=values['allocated']
    if native:
        protocol.validate(plan['allocation'],check_code=False)
        if (plan['allocation']['operation']!='allocate' or plan['allocation']['distribution']!=bound['distribution']
                or plan['allocation']['arguments']!={'seed':plan['seed']}
                or any(plan['code'].get(n)!=h for n,h in plan['allocation']['helpers'].items())):
            raise ValueError('Independent kernel allocation changed its selection')
        if prepared is not None:
            control.validate_prepared(prepared)
            if prepared['request']!=plan['allocation']:raise ValueError('Kernel preparation differs from its plan')
        if allocated is not None:
            control.validate_receipt(allocated)
            if allocated['request']!=plan['allocation'] or allocated['preparation']!=prepared:
                raise ValueError('Kernel allocation lacks its exact creator')
        nonce=prepared['native']['reservation']['nonce'] if prepared is not None else None
    else:
        if plan['allocation'] is not None or prepared is not None or allocated is not None or values['admitted'] is not None:
            raise ValueError('Windows-only validator has invented kernel scope evidence')
        nonce=plan['seed']
    selected=values['guardian']
    if selected is not None:
        if set(selected)!={'request','preparation','node_claim'}:raise ValueError('Invalid independent guardian selection')
        request=selected['request'];preparation=selected['preparation']
        guardian.validate(request,check_code=False,check_directory=False);guardian.validate_prepared(request,preparation)
        if (request['owner']!=json.loads(row.owner_identity_json) or request['nonce']!=nonce
                or Path(request['root']['path'])!=storage._fs_root().resolve()/DIRECTORY/nonce
                or any(plan['code'].get(n)!=h for n,h in request['helpers'].items())
                or native and allocated is None):raise ValueError('Independent guardian changed its owner or launch')
        claim=selected['node_claim']
        if (set(claim)!={'root','path','sha256','owner','bytes'} or claim['owner']!=preparation['guardian']
                or claim['bytes']!=guardian.LIMIT*3 or Path(claim['root'])!=storage._fs_root().resolve()
                or not Path(claim['path']).is_relative_to(Path(claim['root']))):raise ValueError('Independent guardian lost future receipt credit')
    intent={'launch_nonce':nonce,'runtime':bound['runtime'],'gate_sha256':plan['code']['process_gate.py'],
        'identity_probe_sha256':plan['code']['process_identity_native.py']}
    if native:intent['distribution']=bound['distribution']
    admitted=values['admitted']
    if admitted is not None:
        if selected is None:raise ValueError('Native preflight has no independently guarded host')
        from .sandbox_image_guardian import native_selection
        native_selection(admitted,dict(intent,host_launch_sha256=plan['code'],native_scope_helpers=plan['code']),bound['distribution'])
        if child['identity'] is not None:
            record=child['identity'];observed=copy.deepcopy(record['native']);observed['process']['uid']=0
            if record['host']!=admitted['host'] or observed!=admitted['native'] or record['native']['process']['uid']!=65534:
                raise ValueError('Native validator changed its preflight birth or unprivileged identity')
    observations=[];missing=False
    for stage in process_preparation.STAGES:
        value=values[stage]
        if value is None:missing=True;continue
        if missing or selected is None or native and admitted is None:raise ValueError('Validator preparation lacks durable admission')
        observations.append(value)
    process_preparation.chain(observations,intent)
    if admitted is not None:
        for observation in observations:
            if observation['host']!=admitted['host']:raise ValueError('Preparation changed its admitted host birth')
            if observation['native'] is not None:
                expected=copy.deepcopy(admitted['native']);expected['process']['uid']=65534
                if observation['native']!=expected:raise ValueError('Preparation changed its same-process dropped identity')
    if child['identity'] is not None and observations:process_preparation.matches_record(observations[-1],child['identity'])
    closed=values['closed']
    if closed is not None:
        if set(closed)!={'history_sha256','guardian','native'} or closed['history_sha256']!=digest({k:v for k,v in values.items() if k!='closed'}):
            raise ValueError('Independent closure lost its complete preparation history')
        from .sandbox_image_guardian import validate_proof
        if selected is None:raise ValueError('Independent closure has no guardian selection')
        validate_proof({k:selected[k] for k in ('request','preparation')},closed['guardian'])
        if native:
            control.validate_receipt(closed['native'])
            receipt=closed['native'];operation='retire' if allocated is not None else 'abandon'
            arguments={'scope':allocated['result']} if allocated is not None else {'allocation_request':plan['allocation'],'preparation':prepared['native']}
            if receipt['request']['operation']!=operation or receipt['request']['distribution']!=bound['distribution'] or receipt['request']['arguments']!=arguments:
                raise ValueError('Native closure changed its exact allocation')
        elif closed['native'] is not None:raise ValueError('Windows validator has invented Linux closure')
    return values


async def write(owner,child_id,stage,value,*,opening=False):
    from .case_validation import admit_session_locked
    async with SessionLocal() as db:
        if opening:await admit_session_locked(db,NativeRequest,owner.id)
        else:
            selected=await db.get(NativeRequest,owner.id)
            if selected is None:raise ValueError('Independent scope owner disappeared')
            await lock_case_execution(db,selected.case_id)
        row=await records._current(db,owner,owner.nonce,launch=opening)
        if opening:
            from .job_output_finalizing import require_native_release
            await require_native_release(db,row)
        child=records.document(row)['children'][child_id]
        if opening:
            from .finalizing_native_preparation import require_open
            await require_open(db,row,child,stage,value)
        previous=await db.get(DurableEvent,event_id(child_id,stage))
        payload={'binding':binding(row,child),'value':value}
        if previous is not None:
            if json.loads(previous.payload_json)!=payload:raise ValueError('Independent scope stage already differs')
            return
        await append_event(db,row.case_id,'job.finalizer_scope_'+stage,payload,event_id=event_id(child_id,stage))
        await db.flush();await read(db,row,child);await db.commit()


async def prepare(executor,entry):
    if os.name!='nt':
        from .finalizing_linux_scope import prepare as linux_prepare
        return await linux_prepare(executor,entry)
    process=entry.owned;owner=executor.owner;native=executor.runtime=='wsl'
    def active():
        executor.execution_budget.check();entry.check()
        if entry.event.is_set() or not executor._allowed(entry.caller):raise ValueError('Independent validator stopped during preparation')
    active()
    controller=None
    if native:
        async def prepared(value):
            await write(owner,entry.id,'prepared',value,opening=True)
            process.nonce=value['native']['reservation']['nonce'];active()
        controller=control.ScopeControl(executor.recovery_profile['distro'],'allocate',{'seed':process.nonce},
            on_prepared=prepared,check_active=active)
    await write(owner,entry.id,'plan',{'seed':process.nonce,'code':code(),'allocation':copy.deepcopy(controller.request) if controller else None},opening=True)
    if controller:
        receipt=await controller.run();await finish_before_cancel(write(owner,entry.id,'allocated',receipt))
        process.native_scope={'scope':copy.deepcopy(receipt['result']),
            'helper_sha256':receipt['request']['helpers']['process_cgroup_native.py'],'drop_uid':65534}
    active()
    from . import node_storage_budget as capacity
    root=storage._fs_root().resolve()/DIRECTORY/process.nonce
    async with capacity.allocation(guardian.LIMIT*4):
        await finish_before_cancel(asyncio.to_thread(root.parent.mkdir,parents=True,exist_ok=True))
        claims=[]
        def created(birth):claims.append(capacity._admit(guardian.LIMIT*3,owner=birth))
        await process.prepare_guardian(root,max_seconds=min(86430,max(1,math.ceil(entry.deadline-time.monotonic())+30)),on_created=created)
        claim=claims[0]
        await write(owner,entry.id,'guardian',{'request':copy.deepcopy(process.guardian.request),
            'preparation':copy.deepcopy(process.guardian.prepared),
            'node_claim':{'root':str(claim[0]),'path':str(claim[1]),'sha256':hashlib.sha256(claim[2]).hexdigest(),
                'owner':process.guardian.prepared['guardian'],'bytes':guardian.LIMIT*3}},opening=True)
    active()
    async def observed(value):
        await write(owner,entry.id,value['stage'],value,opening=True);active()
    process.on_preparation=observed
    if native:
        async def admitted(value):await write(owner,entry.id,'admitted',value,opening=True);active()
        process.on_native_preflight=admitted


async def require_ready(db,row,child,record):
    from . import finalizing_linux_scope as linux
    if linux.applies(row):return await linux.require_ready(db,row,child,record)
    value=await read(db,row,child)
    if value is None or value['guardian'] is None or value['closed'] is not None:
        raise ValueError('Independent validator has no open owned scope')
    if records.is_native(row) and value['admitted'] is None:raise ValueError('Independent native validator was not admitted')
    stage='native_ready' if records.is_native(row) else 'host_ready'
    if value[stage] is None:raise ValueError('Independent validator lacks observed preparation')
    process_preparation.matches_record(value[stage],record)


async def retirement(value):
    if value['prepared'] is None:return None
    request=value['plan']['allocation'];allocated=value['allocated']
    operation='retire' if allocated is not None else 'abandon'
    arguments={'scope':allocated['result']} if allocated is not None else {'allocation_request':request,'preparation':value['prepared']['native']}
    return await control.control(request['distribution'],operation,arguments)


async def close(owner,child_id,*,process):
    if os.name!='nt':
        from .finalizing_linux_scope import close as linux_close
        return await linux_close(owner,child_id,process=process)
    async with SessionLocal() as db:
        row=await records._current(db,owner,owner.nonce);child=records.document(row)['children'][child_id]
        value=await read(db,row,child)
    if value is None or value['closed'] is not None:return
    proof=None
    try:proof=await process.retire_guardian()
    finally:native=await retirement(value)
    if value['guardian'] is None:raise ValueError('Partial independent launch needs explicit recovery')
    await write(owner,child_id,'closed',{'history_sha256':digest({k:v for k,v in value.items() if k!='closed'}),
        'guardian':proof,'native':native})


async def recover(row,*,cancelled=False,terminal=False):
    """Observe old owner exit, drain its exact scopes, then preserve receipts."""
    from . import finalizing_linux_scope as linux
    if linux.applies(row):return await linux.recover(row,cancelled=cancelled,terminal=terminal)
    from .case_validation import assert_writable_locked
    from .sandbox_image_guardian import recover as recover_guardian
    before=fingerprint(row);children=records.document(row)['children'];proofs={}
    if cancelled and terminal:raise ValueError('Independent retirement has two owner authorities')
    if terminal:
        from .finalizing_native_preparation import terminal_digest
        terminal_digest(row)
    elif cancelled:
        from .finalizing_request_cancellation import require_fence
        async with SessionLocal() as db:await require_fence(db,row)
    elif await asyncio.to_thread(identity.probe,json.loads(row.owner_identity_json))!={'state':'exited','reason':'process_exited'}:
        raise ValueError('Independent validator owner has not provably exited')
    for child_id,child in children.items():
        async with SessionLocal() as db:value=await read(db,row,child)
        if child['identity'] is None:
            from . import finalizing_native_preparation as preparation
            async with SessionLocal() as db:intended=await preparation.intent(db,row,child)
            if intended is not None:
                proofs[child_id]=await preparation.recover(row,child,value,cancelled=cancelled,terminal=terminal);continue
        if value is None or value['guardian'] is None:
            raise ValueError('Historical or partial validator has no independent complete ownership evidence')
        if value['closed'] is not None:proofs[child_id]=value['closed'];continue
        if child['identity'] is None:raise ValueError('Partial independent preparation still requires disposition')
        proof=await recover_guardian({k:value['guardian'][k] for k in ('request','preparation')})
        native=await retirement(value)
        birth=child['identity']
        if await asyncio.to_thread(identity.probe,birth['host'])!={'state':'exited','reason':'process_exited'}:
            raise ValueError('Independent validator host exit is unknown')
        if birth['native'] is not None:
            if await owned_process.probe_native(birth['native']['distribution'],birth['native']['process'])!={'state':'exited','reason':'process_exited'}:
                raise ValueError('Independent native validator exit is unknown')
        closed={'history_sha256':digest({k:v for k,v in value.items() if k!='closed'}),'guardian':proof,'native':native}
        async with SessionLocal() as db:
            await lock_case_execution(db,row.case_id);await assert_writable_locked(db,row.case_id)
            current=await db.get(NativeRequest,row.id,populate_existing=True)
            if current is None or fingerprint(current)!=before:raise ValueError('Independent request changed during scope retirement')
            current_value=await read(db,current,child)
            if current_value['closed'] is not None:closed=current_value['closed']
            else:
                if current_value!=value:raise ValueError('Independent scope changed during retirement')
                await append_event(db,row.case_id,'job.finalizer_scope_closed',{'binding':binding(current,child),'value':closed},event_id=event_id(child_id,'closed'))
                await db.flush();await read(db,current,child);await finish_before_cancel(db.commit())
        proofs[child_id]=closed
    return proofs


async def require_closed(db,row):
    for child in records.document(row)['children'].values():
        value=await read(db,row,child)
        if value is None or value['closed'] is None:raise ValueError('Independent validator scope has no actual closure proof')
