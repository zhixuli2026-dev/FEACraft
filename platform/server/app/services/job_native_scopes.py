"""Durable kernel scopes for engineering children and sealed native shell roots."""
import hashlib,json
from pathlib import Path
from sqlalchemy import select
from ..db import SessionLocal
from ..models import DurableEvent,Job,JobAttempt
from . import job_execution,job_worker_identity,process_record
from . import native_scope_control as control,native_scope_controller as protocol,process_cgroup_native as groups
from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import json_text,lock_case_execution

SCHEMA='vibeflow.job-native-scope.v1'
ROOT_SCHEMA='vibeflow.job-sandbox-root-scope.v1'
WSL_ROOT_SCHEMA='vibeflow.job-unprivileged-wsl-root-scope.v1'
INTENT='attempt.native_scope_intent'
PREPARED='attempt.native_scope_prepared'
ALLOCATED='attempt.native_scope_allocated'
ADMITTED='attempt.native_scope_admitted'
RETIRED='attempt.native_scope_retired'
KINDS={INTENT,PREPARED,ALLOCATED,ADMITTED,RETIRED}
RECOVERED='attempt.native_scope_recovered'


def root_policy(job,attempt):
    from .job_scope_guardians import sealed_sandbox_shell,sealed_wsl_shell
    if sealed_sandbox_shell(job,attempt):return ROOT_SCHEMA,'wsl-sandbox',0
    if sealed_wsl_shell(job,attempt):return WSL_ROOT_SCHEMA,'wsl',65534
    raise ValueError('Root native allocation requires a sealed controlled shell')


def expected_uid(bound):
    return 0 if bound['schema']==ROOT_SCHEMA else 65534


def allocation_arguments(seed, policy):
    arguments={'seed':seed}
    if policy is not None and any(policy.values()):
        arguments['compute_policy']=policy
    return arguments


def child_compute_policy(policy, usage):
    remaining=policy['cpu_limit_ns']
    if remaining:
        remaining-=usage.get('cpu_used_ns',0)
        if remaining<=0:
            raise ValueError('The WSL job has no remaining CPU allowance')
    return {'cpu_limit_ns':remaining,'memory_limit_bytes':policy['memory_limit_bytes']}


def merge_compute_usage(total, usage):
    for field,value in usage.items():
        if field in {'cpu_used_ns','memory_oom_kills'}:
            total[field]=total.get(field,0)+value
        else:
            total[field]=max(total.get(field,0),value)


def binding(job,attempt,child_id,parent_nonce,distribution,seed):
    schema=SCHEMA
    if child_id is None:
        schema,_,_=root_policy(job,attempt)
        if parent_nonce is not None:raise ValueError('Root native allocation cannot name a parent')
    else:
        parent=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
        if parent['launch_nonce']!=parent_nonce:raise ValueError('Native allocation changed its parent')
    return {'schema':schema,'case_id':job.case_id,'job_id':job.id,'attempt_id':attempt.id,
        'child_id':child_id,'parent_nonce':parent_nonce,'distribution':distribution,'seed':seed,
        'worker':job_worker_identity.document(job,attempt)}


async def record(owner,kind,bound,value,*,opening=False):
    if kind not in KINDS:raise ValueError('Unknown native scope stage')
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        if bound!=binding(job,attempt,bound['child_id'],bound['parent_nonce'],bound['distribution'],bound['seed']):
            raise ValueError('Native allocation owner changed')
        if opening:
            from .job_write_checkpoint import assert_native_open_locked
            await assert_native_open_locked(db,job,attempt)
        existing=list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==kind)))
        selected=[json.loads(v) for v in existing if json.loads(v)['binding']['child_id']==bound['child_id']]
        event={'binding':bound,'value':value}
        if selected:
            if selected!=[event]:raise ValueError('Native scope stage cannot be rebound')
            return
        await job_execution.attempt_event(db,job,attempt,kind,event);await db.commit()


async def allocate(owner,parent_nonce,entry,distribution,*,check_active,compute_policy=None):
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        bound=binding(job,attempt,entry.id,parent_nonce,distribution,entry.owned.nonce)
    from .job_allocation_recovery import contract
    await record(owner,INTENT,bound,contract() if entry.id is None else {},opening=True)
    entry.job_native_scope={'binding':bound,'prepared':None,'allocated':None,'admitted':None,'retired':None}
    async def preparing(value):
        control.validate_prepared(value)
        await record(owner,PREPARED,bound,value,opening=True)
        entry.job_native_scope['prepared']=value
    arguments=allocation_arguments(bound['seed'],compute_policy)
    receipt=await control.ScopeControl(distribution,'allocate',arguments,
        on_prepared=preparing,check_active=check_active).run()
    control.validate_receipt(receipt)
    entry.job_native_scope['allocated']=receipt
    await finish_before_cancel(record(owner,ALLOCATED,bound,receipt))
    selected=groups.validate(receipt['result'])
    entry.owned.nonce=selected['nonce']
    entry.owned.native_scope={'scope':selected,'helper_sha256':hashlib.sha256(Path(groups.__file__).read_bytes()).hexdigest()}
    if 'compute' in selected:
        entry.owned.native_scope['compute_helper_sha256']=receipt['request']['helpers']['native_compute_v1.py']
    # The fixed sandbox launcher needs mount capabilities before it confines
    # user code. Registered engineering children run directly as uid 65534.
    if expected_uid(bound)==65534:entry.owned.native_scope['drop_uid']=65534
    return selected


async def admitted(owner,entry):
    state=getattr(entry,'job_native_scope',None)
    if state is None:return
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        value=process_record.bind(entry.owned.record,job,attempt,attempt.worker_id)
    scope=state['allocated']['result']
    if (value is None or value['native'] is None or value['native']['distribution']!=state['binding']['distribution']
            or value['launch_nonce']!=scope['nonce'] or value['native']['process']['uid']!=expected_uid(state['binding'])
            or entry.id is None and value['runtime']!=root_policy(job,attempt)[1]
            or value['native']['process']['boot_id']!=scope['boot_id']):
        raise ValueError('Native admission does not identify the allocated gate')
    observed=await control.control(state['binding']['distribution'],'observe',{'scope':scope})
    control.validate_receipt(observed)
    if observed['result']['state']!='populated':raise ValueError('Prepared native gate is outside its kernel scope')
    proof={'identity':value,'observation':observed}
    await record(owner,ADMITTED,state['binding'],proof,opening=True);state['admitted']=proof


async def retire(owner,entry):
    state=getattr(entry,'job_native_scope',None)
    if state is None or state['retired'] is not None:return
    bound=state['binding'];allocated=state['allocated'];prepared=state['prepared']
    if allocated is not None:
        receipt=await control.control(bound['distribution'],'retire',{'scope':allocated['result']})
    elif prepared is not None:
        receipt=await control.control(bound['distribution'],'abandon',{'allocation_request':prepared['request'],'preparation':prepared['native']})
    else:return
    control.validate_receipt(receipt)
    await record(owner,RETIRED,bound,receipt);state['retired']=receipt


async def public_compute_usage(db, job, attempt, policy):
    from .execution_budget import native_compute_violation

    records=await db.execute(select(DurableEvent.type,DurableEvent.payload_json).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.case_id==job.case_id,DurableEvent.type.in_(KINDS)).order_by(DurableEvent.seq))
    scopes=validate_events(job,attempt,[(kind,json.loads(raw)) for kind,raw in records])
    total={};retired=False;unverified=False
    for state in scopes.values():
        stages=state['stages']
        if RETIRED not in stages or ALLOCATED not in stages or 'compute' not in stages[ALLOCATED]['result']:
            continue
        retired=True
        usage=stages[RETIRED]['result'].get('compute_usage',{})
        merge_compute_usage(total,usage)
        unverified|=not usage and (state['binding']['child_id'] is None or ADMITTED in stages)
    if not retired:
        return None
    violation='compute_unverified' if unverified else native_compute_violation(policy,total)
    return {'schema':'vibeflow.wsl-compute-usage.v1','policy':policy,'violation':violation,**total}


def validate_events(job,attempt,events):
    """Validate immutable stage copies; recovery checks live ownership separately."""
    by_child={};usage={}
    compute_policy=json.loads(attempt.input_manifest_json).get('native_compute_policy')
    for kind,event in events:
        if kind not in KINDS or not isinstance(event,dict) or set(event)!={'binding','value'}:raise ValueError('Malformed native scope event')
        bound=event['binding'];child=bound['child_id'];value=event['value']
        if bound!=binding(job,attempt,child,bound['parent_nonce'],bound['distribution'],bound['seed']):raise ValueError('Native scope history changed its owner')
        stages=by_child.setdefault(child,{'binding':bound,'stages':{}})
        if stages['binding']!=bound or kind in stages['stages']:raise ValueError('Native scope history duplicated/rebound a stage')
        prior=stages['stages']
        if kind==INTENT:
            if prior:raise ValueError('Native allocation did not start with its intent')
            if value!={}:
                from .job_allocation_recovery import validate_contract
                validate_contract(value)
        elif kind==PREPARED:
            if set(prior)!={INTENT}:raise ValueError('Native preparation is out of order')
            control.validate_prepared(value);request=value['request']
            policy=compute_policy
            if child is not None and policy and any(policy.values()):
                policy=child_compute_policy(policy,usage)
            if (request['operation']!='allocate' or request['distribution']!=bound['distribution']
                    or request['arguments']!=allocation_arguments(bound['seed'],policy)):
                raise ValueError('Native preparation changed its allocation')
        elif kind==ALLOCATED:
            if set(prior)!={INTENT,PREPARED}:raise ValueError('Native allocation is out of order')
            control.validate_receipt(value)
            if value['preparation']!=prior[PREPARED] or value['request']!=prior[PREPARED]['request']:raise ValueError('Native allocation changed its prepared creator')
        elif kind==ADMITTED:
            if set(prior)!={INTENT,PREPARED,ALLOCATED}:raise ValueError('Native admission is out of order')
            record_value=value['identity'];process_record.for_attempt(record_value,job,attempt);scope=prior[ALLOCATED]['result']
            if (record_value['launch_nonce']!=scope['nonce'] or record_value['native'] is None
                    or record_value['native']['process']['uid']!=expected_uid(bound)
                    or child is None and record_value['runtime']!=root_policy(job,attempt)[1]):
                raise ValueError('Native gate changed scope/identity')
            control.validate_receipt(value['observation'])
            if (value['observation']['request']['operation']!='observe' or value['observation']['request']['distribution']!=bound['distribution']
                    or value['observation']['request']['arguments']!={'scope':scope} or value['observation']['result']['state']!='populated'
                    or record_value['native']['distribution']!=bound['distribution'] or record_value['native']['process']['boot_id']!=scope['boot_id']):
                raise ValueError('Native gate admission lacks its populated scope')
        elif kind==RETIRED:
            if PREPARED not in prior:raise ValueError('Native retirement has no creation proof')
            control.validate_receipt(value)
            if ALLOCATED in prior:
                if value['request']['operation']!='retire' or value['request']['arguments']!={'scope':prior[ALLOCATED]['result']}:raise ValueError('Native retirement changed scope')
            elif (value['request']['operation']!='abandon' or value['request']['arguments']!={'allocation_request':prior[PREPARED]['request'],'preparation':prior[PREPARED]['native']}):
                raise ValueError('Unallocated scope requires creator-bound abandon')
            if value['request']['distribution']!=bound['distribution']:raise ValueError('Native retirement changed distribution')
            if child is not None and ALLOCATED in prior and 'compute' in prior[ALLOCATED]['result']:
                merge_compute_usage(usage,value['result'].get('compute_usage',{}))
        prior[kind]=value
    return by_child


def snapshot(attempt):
    return (attempt.input_manifest_json,attempt.process_identity_json,attempt.child_processes_json,attempt.worker_identity_json)


def match_observed_root(job,attempt,state,record,registration,child_preparations=None):
    """An observed waiting root can lose its admission acknowledgement.

    Allocation and the original complete observation must still identify the
    same controlled gate. Recovery revokes the entire allocated scope, then
    checks this gate exited; it never asserts a missing admission happened.
    """
    from .job_root_preparation import process,SCOPE_VIEW
    stages=state['stages'];bound=state['binding'];scope=stages[ALLOCATED]['result']
    if bound['child_id'] is not None:
        from .child_launch_registration import prepared_identity,PREPARED as CHILD_PREPARED
        from .native_child_records import document
        child=document(job,attempt)['children'][bound['child_id']]
        entries=(child_preparations or {}).get(child['id'])
        if (not entries or any(kind in {ADMITTED,CHILD_PREPARED} for kind,_ in entries)
                or ADMITTED in stages or prepared_identity(job,attempt,child,entries)!=record
                or record['runtime']!='wsl' or record['launch_nonce']!=scope['nonce']
                or entries[1][1]['distribution']!=bound['distribution']
                or child['parent_launch_nonce']!=bound['parent_nonce']):
            raise ValueError('Observed child does not identify its original allocated kernel scope')
        if record['schema']==SCOPE_VIEW:return
        if (record['native'] is None or record['native']['distribution']!=bound['distribution']
                or record['native']['process']['uid']!=expected_uid(bound)
                or record['native']['process']['boot_id']!=scope['boot_id']):
            raise ValueError('Observed child changed its original native gate')
        return
    if (ADMITTED in stages or bound['child_id'] is not None or registration is None
            or registration['prepared'] is not None or registration['admission'] is not None
            or process(job,attempt,registration)!=record
            or record['launch_nonce']!=scope['nonce']
            or record['runtime']!=root_policy(job,attempt)[1]):
        raise ValueError('Observed root does not identify its original allocated kernel scope')
    if record['schema']==SCOPE_VIEW:
        if record['native'] is not None:raise ValueError('Partial scope view invents native readiness')
        return
    if (record['native'] is None or record['native']['distribution']!=bound['distribution']
            or record['native']['process']['uid']!=expected_uid(bound)
            or record['native']['process']['boot_id']!=scope['boot_id']):
        raise ValueError('Observed root does not identify its original allocated kernel scope')


async def recover(job_id,guardians):
    """Revoke a completely admitted old scope after exact owner/host closure."""
    from .native_child_records import document
    from .owned_process import probe_native
    from .job_scope_guardians import SCHEMA as GUARD_SCHEMA
    if not guardians:return []
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        if job is None:return []
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
        if attempt is None:return []
        try:
            worker=job_worker_identity.document(job,attempt)
            if identity.probe(worker['process'])!={'state':'exited','reason':'process_exited'}:return []
            children=document(job,attempt)['children']
            from .child_launch_registration import prepared_histories,validate_preparations
            try:
                preparations=await prepared_histories(db,job,attempt)
                from .child_handoff_recovery import histories as unlaunched_histories
                unlaunched=await unlaunched_histories(db,job,attempt)
                prepared_children=validate_preparations(job,attempt,preparations,unlaunched=unlaunched)
            except (ValueError,KeyError,TypeError):preparations=None;prepared_children={}
            from .job_root_preparation import read as read_root,process as root_process
            root_preparation=await read_root(db,job,attempt)
            root_identity=root_process(job,attempt,root_preparation) if root_preparation is not None else json.loads(attempt.process_identity_json)
            rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type.in_(KINDS)).order_by(DurableEvent.seq)))
            events=[(r.type,json.loads(r.payload_json)) for r in rows];allocated=validate_events(job,attempt,events)
        except (ValueError,KeyError,TypeError):return []
        before=snapshot(attempt)
        existing=list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==RECOVERED)))
        prior={json.loads(v)['child_id']:json.loads(v) for v in existing}
    root_guard=next((v for v in guardians if v.get('schema')==GUARD_SCHEMA and v.get('child_id') is None and v.get('host_scope_verified')),None)
    if root_guard is None:return []
    results=[]
    for child_id,state in allocated.items():
        stages=state['stages'];child=children.get(child_id)
        if child is not None and child['identity'] is None and child_id in prepared_children:
            # Resolve only for this independent recovery. The durable journal
            # continues to show that running registration never committed.
            child={**child,'identity':prepared_children[child_id]}
        if child_id is None:
            child={'identity':root_identity,
                'stop':json.loads(attempt.output_manifest_json).get('process_stop')}
        guard=next((v for v in guardians if v.get('child_id')==child_id and v.get('host_scope_verified')),None)
        if child is None or child['identity'] is None or guard is None:continue
        admission=stages.get(ADMITTED)
        if admission is not None:
            if admission['identity']!=child['identity']:continue
        else:
            try:match_observed_root(job,attempt,state,child['identity'],root_preparation,preparations)
            except (ValueError,KeyError,TypeError):continue
        if child_id in prior:results.append(prior[child_id]);continue
        scope=stages[ALLOCATED]['result'];distribution=state['binding']['distribution']
        # An earlier child may already have completed on this same attempt.
        # Retain its exact original kernel receipt instead of omitting its
        # scope or issuing a second retirement. Owner, guardian and gate exit
        # are still independently observed for this recovered inventory.
        receipt=stages[RETIRED] if RETIRED in stages else await control.control(distribution,'retire',{'scope':scope})
        control.validate_receipt(receipt)
        known_native=child['identity']['native']
        gate=await probe_native(distribution,known_native['process']) if known_native is not None else None
        if known_native is not None and gate!={'state':'exited','reason':'process_exited'}:continue
        value={'schema':state['binding']['schema'],'case_id':job.case_id,'job_id':job.id,'attempt_id':attempt.id,'child_id':child_id,
            'history_sha256':hashlib.sha256(json_text(events).encode()).hexdigest(),
            'binding':state['binding'],'allocation':stages[ALLOCATED],'admission':admission,
            'retirement':receipt,'native_gate_exit':gate,'worker_exit':{'state':'exited','reason':'process_exited'},
            'root_guardian_sha256':hashlib.sha256(json_text(root_guard).encode()).hexdigest(),
            'child_guardian_sha256':hashlib.sha256(json_text(guard).encode()).hexdigest(),
            'native_tree_verified':True,'original_stop_missing':child['stop'] is None,'drain_certificate_created':False}
        async with SessionLocal() as db:
            await lock_case_execution(db,job.case_id)
            fresh=await db.get(JobAttempt,attempt.id,populate_existing=True)
            if fresh is None or snapshot(fresh)!=before:continue
            fresh_job=await db.get(Job,job.id)
            if preparations is not None and await prepared_histories(db,fresh_job,fresh)!=preparations:continue
            if root_preparation is not None and await read_root(db,fresh_job,fresh)!=root_preparation:continue
            current=list(await db.scalars(select(DurableEvent.payload_json).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==RECOVERED)))
            matches=[json.loads(v) for v in current if json.loads(v)['child_id']==child_id]
            if matches:
                results.append(matches[0]);continue
            await job_execution.attempt_event(db,fresh_job,fresh,RECOVERED,value);await db.commit()
        results.append(value)
    return results
