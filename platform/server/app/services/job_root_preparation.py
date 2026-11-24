"""Read original Windows/WSL startup evidence without inventing running state."""
import hashlib,json
from sqlalchemy import select
from ..models import DurableEvent
from . import job_launch_registration as launches, job_native_scopes as native
from . import job_worker_identity, process_preparation, process_record

SCOPE_VIEW='vibeflow.root-startup-scope-view.v1'


def history_digest(value):
    from .durable_events import json_text
    # Cancellation can arrive after an independent observation. Its stamp is
    # checked separately; immutable launch facts must never be truncated or
    # promoted to a later handshake when the proof is transported.
    return hashlib.sha256(json_text({key:value[key] for key in (
        'intent','prepared','observations','admission')}).encode()).hexdigest()


def process(job, attempt, value):
    fields={'original_phase','cancel_requested_at','intent','prepared','observations','admission'}
    if (not isinstance(value,dict) or set(value)!=fields
            or value['original_phase'] not in {'starting','cancelling'}
            or json.loads(attempt.process_identity_json)!={} or json.loads(attempt.child_processes_json)!={}
            or job_worker_identity.document(job,attempt)['process']['platform']!='windows'):
        raise ValueError('Root startup recovery changed original registration facts')
    cancelled=value['original_phase']=='cancelling'
    stamp=attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None
    if value['cancel_requested_at']!=stamp or cancelled!=(stamp is not None):
        raise ValueError('Root startup recovery changed the cancellation request')
    intent=launches.validate_intent(value['intent'],job,attempt)
    latest=process_preparation.chain(value['observations'],intent)
    prepared=value['prepared'];admission=value['admission']
    if prepared is not None:
        if not isinstance(prepared,dict) or set(prepared)!={'identity'} or admission is not None:
            raise ValueError('Ambiguous original root preparation')
        record=prepared['identity']
    elif admission is not None:
        if not isinstance(admission,dict) or set(admission)!={'binding','value'}:
            raise ValueError('Root startup lacks completed preparation or native admission')
        bound=admission['binding'];observed=admission['value']
        if (bound!=native.binding(job,attempt,None,None,bound['distribution'],bound['seed'])
                or not isinstance(observed,dict) or set(observed)!={'identity','observation'}):
            raise ValueError('Root startup changed its original kernel admission')
        record=observed['identity']
        if record['native'] is None:raise ValueError('Native admission lacks a native root')
    else:
        if latest is None or latest['host']['platform']=='windows' and (
                latest['stage']=='created' or intent['runtime']!='windows' and latest['stage']=='host_ready'):
            # Scope selection is known from the original launch intent and
            # checked against the independent guardian. This is deliberately
            # not a process_record: no completed handshake/native birth is
            # inferred from creation. Kernel revocation fences a late WSL join.
            return {'schema':SCOPE_VIEW,'launch_nonce':intent['launch_nonce'],'runtime':intent['runtime'],
                'host':latest['host'] if latest is not None else None,'native':None,
                'scope':'Local\\VibeFlow-'+intent['launch_nonce'],
                'original_stage':latest['stage'] if latest is not None else 'unobserved'}
        # Project only fields already observed by the original waiting gate.
        # This local view is never written as a prepared/running event. The
        # proof retains both missing acknowledgements and the original chain.
        if latest is None or latest['stage']!=('host_ready' if intent['runtime']=='windows' else 'native_ready'):
            raise ValueError('Root startup lacks a complete original gate observation')
        record=process_record.bind({'schema':process_record.SCHEMA,'pid':latest['host']['pid'],
            **{key:latest[key] for key in process_record.BASE-{'schema','pid'}}},job,attempt,attempt.worker_id)
    launches.match(intent,record,job,attempt)
    if record['host']['platform']!='windows' or latest is None:
        raise ValueError('Root preparation lacks complete Windows host observations')
    process_preparation.matches_record(latest,record)
    return record


async def read(db,job,attempt):
    if json.loads(attempt.process_identity_json)!={}:return None
    if attempt.phase not in {'starting','cancelling'}:return None
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type.in_({launches.INTENT,launches.OBSERVED,launches.PREPARED,launches.ABORTED,native.ADMITTED})
        ).order_by(DurableEvent.seq)))
    intent=None;prepared=None;admission=None;observations=[]
    for row in rows:
        if row.case_id!=job.case_id:raise ValueError('Root preparation crosses case ownership')
        payload=json.loads(row.payload_json)
        if row.type==launches.INTENT:
            if intent is not None or observations or prepared or admission:raise ValueError('Root intent reordered or duplicated')
            intent=payload
        elif row.type==launches.OBSERVED:
            if intent is None or prepared is not None or admission is not None:raise ValueError('Root observation reordered')
            observations.append(payload)
        elif row.type==launches.PREPARED:
            if intent is None or prepared is not None:raise ValueError('Root preparation reordered or duplicated')
            prepared=payload
        elif row.type==native.ADMITTED:
            if intent is None or prepared is not None or admission is not None:raise ValueError('Root admission reordered or duplicated')
            admission=payload
        else:raise ValueError('Root startup already has original aborted cleanup')
    value={'original_phase':attempt.phase,
        'cancel_requested_at':attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None,
        'intent':intent,'prepared':prepared,'observations':observations,
        'admission':admission if prepared is None else None}
    process(job,attempt,value)
    return value
