"""Audited drain certificates from independent host and kernel scope witnesses.

Version four retains the original missing stop fields. It certifies that this
observer proved scopes revoked after exact owner death, never that the original
worker completed its missing callbacks or supplied an exit code/control frame.
"""
from datetime import datetime,timedelta
import asyncio,hashlib,json,logging,os
from sqlalchemy import select
from ..db import SessionLocal
from ..models import Case,DurableEvent,Job,JobAttempt
from . import job_execution,job_worker_identity,process_record,native_child_records
from . import job_scope_guardians as guards,job_native_scopes as native
from . import native_scope_control as controller,windows_scope_guardian as windows
from . import process_identity_native as identity
from .durable_events import json_text,lock_case_execution,now

SCHEMA='vibeflow.job-write-checkpoint.v4'
CERTIFICATE='vibeflow.job-scope-recovery.v1'
CHILD_CERTIFICATE='vibeflow.job-child-startup-recovery.v1'
CHILD_HANDOFF_CERTIFICATE='vibeflow.job-child-handoff-recovery.v1'
ROOT_CERTIFICATE='vibeflow.job-root-startup-recovery.v1'
EVENT='attempt.content_scope_recovered'
EXIT={'state':'exited','reason':'process_exited'}
FIELDS={'schema','case_id','job_id','attempt_id','case_prefix','claim_sha256','state',
    'process_identity_json','child_processes_json','stop','recorded_at','recovery'}


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()


def snapshot(case,job,attempt):
    from .job_drain_recovery import snapshot as original
    return original(case,job,attempt)


def validate_certificate(case,job,attempt,value):
    if isinstance(value,dict) and isinstance(value.get('recovery'),dict) and value['recovery'].get('schema') in {
            'vibeflow.job-allocation-recovery.v1','vibeflow.job-launch-preparation-recovery.v1'}:
        from .job_allocation_recovery import validate_certificate as validate_allocation
        return validate_allocation(case,job,attempt,value)
    if isinstance(value,dict) and isinstance(value.get('recovery'),dict) and value['recovery'].get('schema') in {
            'vibeflow.job-linux-scope-recovery.v1','vibeflow.job-linux-startup-recovery.v1'}:
        from .job_linux_namespaces import validate_certificate as validate_linux
        return validate_linux(case,job,attempt,value)
    from .job_write_checkpoint import claim_digest
    from .attempt_workspace import require_sealed
    from .step_handlers import validate_binding
    try:
        require_sealed(job,attempt)
        if (validate_binding(job,attempt) is None and not guards.sealed_windows_command(job,attempt)
                and not guards.sealed_native_shell(job,attempt)):
            raise ValueError('Scope recovery requires a registered private execution or sealed controlled command')
    except job_execution.ExecutionConflict as error:raise ValueError('Recovered private execution binding is invalid') from error
    if (not isinstance(value,dict) or set(value)!=FIELDS or value['schema']!=SCHEMA
            or type(value['case_id']) is not int or type(value['job_id']) is not int
            or value['case_id']!=case.id or value['job_id']!=job.id or value['attempt_id']!=attempt.id
            or job.case_id!=case.id or attempt.job_id!=job.id or value['case_prefix']!=case.s3_prefix
            or value['claim_sha256']!=claim_digest(attempt) or value['state']!='recovered_stopped'
            or value['stop'] is not None or value['process_identity_json']!=attempt.process_identity_json
            or value['child_processes_json']!=attempt.child_processes_json):
        raise ValueError('Recovered drain changed its original attempt or missing-stop facts')
    stamp=datetime.fromisoformat(value['recorded_at'])
    if stamp.utcoffset()!=timedelta(0):raise ValueError('Recovered drain requires UTC')
    proof=value['recovery']
    required={'schema','snapshot_sha256','observer','worker','worker_exit','guardians','native_history',
        'native_retirements','original_output_manifest_json','original_root_stop_missing','original_child_stop_missing'}
    handoff=isinstance(proof,dict) and proof.get('schema')==CHILD_HANDOFF_CERTIFICATE
    starting=isinstance(proof,dict) and proof.get('schema') in {CHILD_CERTIFICATE,CHILD_HANDOFF_CERTIFICATE}
    root_starting=isinstance(proof,dict) and proof.get('schema')==ROOT_CERTIFICATE
    registration=proof.get('root_preparation') if root_starting else None
    cancelled=(root_starting and isinstance(registration,dict) and registration.get('original_phase')=='cancelling') or (not root_starting and attempt.phase=='cancelled')
    target,reason=('cancelled','cancel_requested') if cancelled else ('interrupted','recovery_required')
    if starting:required.add('child_preparations')
    if handoff:required.add('child_handoff_closures')
    if root_starting:required.add('root_preparation')
    if cancelled and not root_starting:
        required.add('cancel_requested_at')
        if attempt.cancel_requested_at is None or proof.get('cancel_requested_at')!=attempt.cancel_requested_at.isoformat():
            raise ValueError('Recovered scope changed the original cancellation request')
    if (not isinstance(proof,dict) or set(proof)!=required or proof['schema'] not in {CERTIFICATE,CHILD_CERTIFICATE,CHILD_HANDOFF_CERTIFICATE,ROOT_CERTIFICATE}
            or proof['snapshot_sha256']!=digest(snapshot(case,job,attempt)) or proof['worker_exit']!=EXIT
            or proof['worker']!=job_worker_identity.document(job,attempt)
            or json.loads(proof['original_output_manifest_json'])!={} or proof['original_root_stop_missing'] is not True
            or json.loads(attempt.output_manifest_json).get('process_stop') is not None
            or job.status!=attempt.phase or attempt.phase!=target or attempt.reason_code!=reason):
        raise ValueError('Recovered drain no longer matches the interrupted private execution')
    observer=identity.validate(proof['observer']);worker=proof['worker']['process']
    if observer['platform']!='windows' or observer['host_id']!=worker['host_id'] or observer==worker:
        raise ValueError('Recovered drain lacks an independent same-host observer')
    if root_starting:
        from .job_root_preparation import process as root_process
        parent=root_process(job,attempt,registration)
    else:parent=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
    if parent['native'] is not None and not guards.sealed_native_shell(job,attempt):
        raise ValueError('Direct native root recovery requires its separate kernel admission')
    children=native_child_records.document(job,attempt)['children']
    preparations=proof['child_preparations'] if starting else None
    closures=proof['child_handoff_closures'] if handoff else {}
    if not isinstance(closures,dict) or handoff and not closures:raise ValueError('Missing child handoff closures')
    prepared_children={}
    if starting:
        from .child_launch_registration import validate_preparations
        if not preparations and not closures:raise ValueError('Child startup recovery lacks original preparations')
        prepared_children=validate_preparations(job,attempt,preparations,unlaunched=closures)
    if proof['original_child_stop_missing']!=sorted(k for k,c in children.items() if c['stop'] is None):
        raise ValueError('Recovered drain rewrote child stop history')
    expected={None,*children}-set(closures);seen={}
    for item in proof['guardians']:
        if not isinstance(item,dict) or set(item)!={'prepared','observed'}:raise ValueError('Malformed guardian certificate')
        bound=item['prepared'];witness=item['observed'];selected=guards.validate(job,attempt,bound,check_directory=False,child_preparations=preparations,root_preparation=registration)
        child=bound['child_id']
        if child in seen or child not in expected:raise ValueError('Duplicate/foreign guardian scope')
        windows.validate_receipt(bound['request'],bound['prepared'],witness['guardian_receipt'],check_directory=False)
        if (witness.get('schema')!=guards.SCHEMA or witness.get('case_id')!=case.id or witness.get('job_id')!=job.id
                or witness.get('attempt_id')!=attempt.id or witness.get('child_id')!=child
                or witness.get('launch_nonce')!=selected['launch_nonce'] or witness.get('prepared_sha256')!=digest(bound)
                or witness.get('worker_exit')!=EXIT or witness.get('guardian_exit')!=EXIT
                or 'host_exit' not in witness or witness['host_exit']!=(EXIT if selected['host'] is not None else None)
                or witness.get('host_scope_verified') is not True or witness.get('drain_certificate_created') is not False):
            raise ValueError('Guardian certificate is not the original closed scope')
        from .job_root_preparation import SCOPE_VIEW
        if (selected['schema']==SCOPE_VIEW or 'host_identity' in witness) and (
                'host_identity' not in witness or witness['host_identity']!=selected['host']):
            raise ValueError('Guardian observation changed the original host birth')
        if root_starting and (selected['schema']==SCOPE_VIEW or 'root_preparation_sha256' in witness):
            from .job_root_preparation import history_digest
            if witness.get('root_preparation_sha256')!=history_digest(registration):
                raise ValueError('Guardian observation changed the original preparation history')
        if child in (preparations or {}):
            entries=preparations[child]
            from .child_launch_registration import PREPARED as CHILD_PREPARED
            early=not any(kind in {CHILD_PREPARED,native.ADMITTED} for kind,_ in entries)
            if (early or 'child_preparation_sha256' in witness) and witness.get('child_preparation_sha256')!=digest(entries):
                raise ValueError('Guardian observation changed the original child preparation history')
        seen[child]=witness
    if set(seen)!=expected:raise ValueError('Recovered drain omits a host scope')
    scopes=native.validate_events(job,attempt,proof['native_history']);retired={}
    closed_native=set()
    from .child_handoff_recovery import validate as validate_child_handoff
    for child,closure in closures.items():
        if closure.get('child_id')!=child:raise ValueError('Child handoff closure changed its inventory key')
        selected_history=validate_child_handoff(job,attempt,closure,seen[None])
        original_history=[item for item in proof['native_history'] if item[1]['binding']['child_id']==child]
        if original_history!=selected_history:raise ValueError('Child handoff omitted original native history')
        if selected_history:closed_native.add(child)
    if root_starting and registration['admission'] is not None and not any(
            kind==native.ADMITTED and payload==registration['admission'] for kind,payload in proof['native_history']):
        raise ValueError('Root identity source differs from its original kernel admission history')
    for entries in (preparations or {}).values():
        if entries[-1][0]==native.ADMITTED and not any(
                kind==native.ADMITTED and payload==entries[-1][1] for kind,payload in proof['native_history']):
            raise ValueError('Child identity source differs from its original kernel admission history')
    native_identities={k:c['identity'] for k,c in children.items()
        if c['identity'] is not None and c['identity']['native'] is not None}
    native_identities.update({k:record for k,record in prepared_children.items() if record['runtime']=='wsl'})
    from .job_root_preparation import SCOPE_VIEW
    partial_native=root_starting and parent['schema']==SCOPE_VIEW and parent['runtime']!='windows'
    if parent['native'] is not None or partial_native:native_identities[None]=parent
    for item in proof['native_retirements']:
        child=item['child_id']
        if child in retired or child not in native_identities:raise ValueError('Duplicate/foreign recovered native scope')
        stages=scopes[child]['stages'];selected=stages[native.ALLOCATED]['result'];admission=stages.get(native.ADMITTED)
        if admission is None:
            native.match_observed_root(job,attempt,scopes[child],native_identities[child],registration,preparations)
        elif admission['identity']!=native_identities[child]:
            raise ValueError('Recovered native gate differs from its original admission')
        original_stop=json.loads(attempt.output_manifest_json).get('process_stop') if child is None else children[child]['stop']
        controller.validate_receipt(item['retirement'])
        if (item.get('schema')!=scopes[child]['binding']['schema'] or item.get('case_id')!=case.id or item.get('job_id')!=job.id
                or item.get('attempt_id')!=attempt.id or item['binding']!=scopes[child]['binding']
                or item['allocation']!=stages[native.ALLOCATED] or item['admission']!=admission
                or item.get('native_tree_verified') is not True
                or 'native_gate_exit' not in item
                or item['native_gate_exit']!=(None if native_identities[child]['native'] is None else EXIT)
                or item.get('worker_exit')!=EXIT
                or item.get('original_stop_missing') is not (original_stop is None)
                or (native.RETIRED in stages and item['retirement']!=stages[native.RETIRED])
                or item.get('history_sha256')!=digest(proof['native_history'])
                or item['retirement']['request']['operation']!='retire'
                or item['retirement']['request']['distribution']!=scopes[child]['binding']['distribution']
                or item['retirement']['request']['arguments']!={'scope':selected}
                or item['retirement']['result']['state']!='absent'
                or item['root_guardian_sha256']!=digest(seen[None]) or item['child_guardian_sha256']!=digest(seen[child])):
            raise ValueError('Recovered native scope changed its original kernel admission')
        retired[child]=item
    if set(retired)!=set(native_identities) or set(scopes)!=set(native_identities)|closed_native:raise ValueError('Recovered drain omits a native scope')
    return value


async def reconcile(job_id,*,starting=False):
    """Commit a distinct recovered drain; never rewrite original stop fields."""
    if os.name!='nt':
        from .job_linux_namespaces import reconcile as reconcile_linux
        return await reconcile_linux(job_id,starting=starting)
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        if job is None:return False
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
        if attempt is None:return False
        if starting and attempt.phase not in {'starting','cancelling'}:return False
        if attempt.write_checkpoint_json!='{}':
            value=json.loads(attempt.write_checkpoint_json)
            if value.get('schema')!=SCHEMA:return False
            validate_certificate(await db.get(Case,job.case_id),job,attempt,value);return True
        if json.loads(attempt.output_manifest_json)!={}:return False
        cancelling=not starting and job.status=='cancelled' and attempt.phase=='cancelling' and attempt.cancel_requested_at is not None
        if starting:
            if attempt.phase=='cancelling':
                if job.status!='cancelled' or attempt.cancel_requested_at is None:return False
            elif job.status not in {'queued','running'} or attempt.cancel_requested_at is not None:return False
        elif not cancelling and (job.status!=attempt.phase or attempt.phase!='interrupted' or attempt.reason_code!='recovery_required'):return False
        from .job_root_preparation import read as read_root
        try:
            worker=job_worker_identity.document(job,attempt)
            if worker['process']['platform']!='windows':return False
            registration=await read_root(db,job,attempt) if starting else None
            if starting and registration is None:return False
        except (ValueError,KeyError,TypeError):return False
        case=await db.get(Case,job.case_id)
        before=snapshot(case,job,attempt);original_output=attempt.output_manifest_json
        cancelled_at=attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None
    from .async_ops import finish_before_cancel
    if await finish_before_cancel(asyncio.to_thread(identity.probe,worker['process']))!=EXIT:return False
    observer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    async with SessionLocal() as db:
        await lock_case_execution(db,job.case_id)
        job=await db.get(Job,job_id,populate_existing=True);attempt=await db.get(JobAttempt,attempt.id,populate_existing=True)
        case=await db.get(Case,job.case_id,populate_existing=True)
        if attempt is None or snapshot(case,job,attempt)!=before or attempt.write_checkpoint_json!='{}':return False
        if (attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None)!=cancelled_at:return False
        if starting:
            if await read_root(db,job,attempt)!=registration:return False
        elif not cancelling and (job.status!=attempt.phase or attempt.phase!='interrupted' or json.loads(attempt.output_manifest_json)!={}):return False
        from .child_launch_registration import prepared_histories
        from . import child_handoff_recovery as handoffs
        try:
            preparations=await prepared_histories(db,job,attempt)
            unlaunched=await handoffs.histories(db,job,attempt)
        except (ValueError,KeyError,TypeError,AttributeError):return False
        case=await db.get(Case,job.case_id,populate_existing=True)
        rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
            DurableEvent.type.in_({guards.PREPARED,guards.OBSERVED,native.RECOVERED,handoffs.EVENT,*native.KINDS})).order_by(DurableEvent.seq)))
        prepared={};observed={};history=[];retirements=[];closures={}
        for row in rows:
            payload=json.loads(row.payload_json)
            if row.type==guards.PREPARED:
                key=payload['child_id']
                if key in prepared:return False
                prepared[key]=payload
            elif row.type==guards.OBSERVED:
                key=payload['child_id']
                if key in observed and observed[key]!=payload:return False
                observed[key]=payload
            elif row.type==native.RECOVERED:retirements.append(payload)
            elif row.type==handoffs.EVENT:
                key=payload['child_id']
                if key in closures:return False
                closures[key]=payload
            else:history.append([row.type,payload])
        if set(closures)!=set(unlaunched) or any(value['history']!=unlaunched[key] for key,value in closures.items()):return False
        if set(prepared)!=set(observed):return False
        if starting or cancelling:
            from .workflow_state import transition
            target,reason=('cancelled','cancel_requested') if cancelling or registration['original_phase']=='cancelling' else ('interrupted','recovery_required')
            await transition(db,'attempt',attempt.id,target,expected_version=attempt.version,
                claim_token=attempt.claim_token,reason_code=reason,
                detail='Original worker and execution scopes exited; original cancellation preserved; execution not replayed' if cancelling else
                    'Original worker and prepared scopes exited before running registration; execution not replayed')
            await job_execution._job_status(db,job,target,None,attempt)
        from .artifact_publication import finalize
        # No bundle/finalization intent was present. Mark only this failed
        # reservation; the previously committed public head remains unchanged.
        await finalize(db,job,attempt,attempt.phase,attempt.reason_code)
        certificate=ROOT_CERTIFICATE if starting else CHILD_HANDOFF_CERTIFICATE if closures else CHILD_CERTIFICATE if preparations else CERTIFICATE
        proof={'schema':certificate,'snapshot_sha256':digest(snapshot(case,job,attempt)),
            'observer':observer,'worker':worker,'worker_exit':EXIT,
            'guardians':[{'prepared':value,'observed':observed[key]} for key,value in prepared.items()],
            'native_history':history,'native_retirements':retirements,
            'original_output_manifest_json':original_output,'original_root_stop_missing':True,
            'original_child_stop_missing':sorted(k for k,c in native_child_records.document(job,attempt)['children'].items() if c['stop'] is None)}
        if preparations or closures:proof['child_preparations']=preparations
        if closures:proof['child_handoff_closures']=closures
        if starting:proof['root_preparation']=registration
        if cancelling:proof['cancel_requested_at']=cancelled_at
        from .job_write_checkpoint import claim_digest
        value={'schema':SCHEMA,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,'case_prefix':case.s3_prefix,
            'claim_sha256':claim_digest(attempt),'state':'recovered_stopped','process_identity_json':attempt.process_identity_json,
            'child_processes_json':attempt.child_processes_json,'stop':None,'recorded_at':now().isoformat(),'recovery':proof}
        try:validate_certificate(case,job,attempt,value)
        except (ValueError,KeyError,TypeError,AttributeError,job_execution.ExecutionConflict) as error:
            logging.getLogger(__name__).warning('Scope drain certificate refused for job %s: %s: %s',job.id,type(error).__name__,str(error))
            await db.rollback();return False
        attempt.write_checkpoint_json=json_text(value)
        await job_execution.attempt_event(db,job,attempt,EVENT,{'schema':certificate,'checkpoint_sha256':digest(value),
            'original_root_stop_missing':True,'original_child_stops_preserved':True,'replayed':False})
        await db.commit();return True
