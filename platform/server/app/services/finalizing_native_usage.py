"""Durable observed usage and bounded uncertainty for an API-owned validator.

Each pipe read is at most WINDOW bytes and commits before the next read.
After owner death the outstanding window stays reserved, never relabeled as
observed output. Diagnostic growth is reconciled from the exact stopped file.
"""
import asyncio,copy,json,stat
from pathlib import Path

from ..db import SessionLocal
from . import native_request_records as records,process_stop
from .durable_events import json_text
from .execution_output_checkpoint import validate_usage

SCHEMA='vibeflow.finalizer-output-progress.v1'
WINDOW=4096


def reserve(row,child_id):
    """Persist the read allowance atomically with the child, before any launch."""
    from .execution_budget import current
    budget=current();profile=json.loads(row.profile_json)
    if budget is None or budget.attempt_id!=profile['attempt_id']:raise ValueError('Child reservation lost original accounting')
    value=copy.deepcopy(validate(json.loads(row.result_json),row,complete=False))
    if child_id in value['channels']:raise ValueError('Child output reservation already exists')
    value['channels'][child_id]={'window':WINDOW,'diagnostic':None,'sealed_stop':None}
    value['sequence']+=1;value['usage']=budget.usage()
    row.result_json=json_text(validate(value,row))


def validate(value,row,*,complete=True):
    from .job_output_finalizing import digest,native_profile
    profile=native_profile(json.loads(row.profile_json))
    if (not isinstance(value,dict) or set(value)!={'schema','profile_sha256','sequence','usage','channels'}
            or value['schema']!=SCHEMA or value['profile_sha256']!=digest(profile)
            or type(value['sequence']) is not int or value['sequence']<=0
            or not isinstance(value['channels'],dict) or len(json_text(value))>1024*1024):
        raise ValueError('Missing durable independent-validator output progress')
    validate_usage(value['usage'],profile['max_log_bytes'])
    children=records.document(row)['children']
    if not set(value['channels'])<=set(children) or complete and set(value['channels'])!=set(children):
        raise ValueError('Validator output progress lacks a child reservation')
    for key,item in value['channels'].items():
        if (not isinstance(item,dict) or set(item)!={'window','diagnostic','sealed_stop'}
                or type(item['window']) is not int or item['window'] not in {0,WINDOW}):
            raise ValueError('Invalid validator read reservation')
        if item['window']==0:
            child=children[key]
            if child['identity'] is None or item['sealed_stop'] is None:
                raise ValueError('A sealed output channel lacks actual stopped identity')
            process_stop.validate(item['sealed_stop'],child['identity'])
            if not item['sealed_stop']['verified']:raise ValueError('Sealed output requires verified physical stop')
        elif item['sealed_stop'] is not None:raise ValueError('Open output channel has a fabricated stop')
        diagnostic=item['diagnostic']
        if diagnostic is not None:
            if (not isinstance(diagnostic,dict) or set(diagnostic)!={'relative','folder_identity','file_identity','seen'}
                    or not isinstance(diagnostic['relative'],str) or type(diagnostic['seen']) is not int or diagnostic['seen']<0):
                raise ValueError('Invalid durable diagnostic observation')
            for field in ('folder_identity','file_identity'):
                pair=diagnostic[field]
                if field=='file_identity' and pair is None:
                    if diagnostic['seen']!=0:raise ValueError('Missing observed diagnostic file identity')
                    continue
                if not isinstance(pair,list) or len(pair)!=2 or any(type(n) is not int or n<0 for n in pair):
                    raise ValueError('Invalid diagnostic inode observation')
    return value


async def checkpoint(executor,entry,diagnostics=None,*,sealed=False):
    if not hasattr(executor,'recovery_profile'):return
    if not hasattr(executor,'usage_lock'):
        executor.usage_lock=asyncio.Lock();executor.usage_meters={}
    async with executor.usage_lock:
        if diagnostics is not None:executor.usage_meters[entry.id]=diagnostics
        for meter in executor.usage_meters.values():meter.sample()
        stamp=json_text({'usage':executor.execution_budget.usage(),'meters':{k:m.checkpoint() for k,m in executor.usage_meters.items()},
            'entry':entry.id,'sealed':sealed})
        if getattr(executor,'usage_checkpoint_stamp',None)==stamp:return
        async with SessionLocal() as db:
            row=await records._current(db,executor.owner,executor.parent_nonce)
            previous=json.loads(row.result_json)
            if previous:validate(previous,row,complete=False)
            from .job_output_finalizing import digest
            value={'schema':SCHEMA,'profile_sha256':digest(executor.recovery_profile),
                'sequence':previous['sequence']+1 if previous else 1,
                'usage':executor.execution_budget.usage(),'channels':copy.deepcopy(previous.get('channels',{}))}
            value['channels'].setdefault(entry.id,{'window':WINDOW,'diagnostic':None,'sealed_stop':None})
            for key,meter in executor.usage_meters.items():
                value['channels'][key]['diagnostic']=meter.checkpoint()
            if sealed and entry.owned.stop_result is not None and entry.owned.stop_result['verified']:
                value['channels'][entry.id].update(window=0,sealed_stop=entry.owned.stop_result)
            validate(value,row,complete=False)
            if previous:
                old=previous['usage'];new=value['usage']
                if (new['observed_output_bytes']<old['observed_output_bytes']
                        or new.get('unsettled_output_bytes',0)!=old.get('unsettled_output_bytes',0)
                        or any(new['by_source'].get(k,0)<n for k,n in old['by_source'].items())):
                    raise ValueError('Validator checkpoint lost spent output credit')
            row.result_json=json_text(value);await db.commit()
        executor.usage_checkpoint_stamp=stamp
        if sealed:executor.usage_meters.pop(entry.id,None)


def reconcile(row,root,proofs=None,*,progress=None):
    """Only after all independently owned native scopes have actually closed."""
    from .attempt_workspace import checked_path,ordinary
    from .finalizing_native_preparation import SCHEMA as PREPARATION,CANCEL_SCHEMA
    from .finalizing_native_scope import digest
    value=validate(json.loads(row.result_json) if progress is None else progress,row)
    from .finalizing_linux_scope import unreleased as linux_unreleased
    unreleased={key:digest(proof) for key,proof in (proofs or {}).items() if proof.get('schema') in {PREPARATION,CANCEL_SCHEMA} or linux_unreleased(proof)}
    observations={}
    for key,item in value['channels'].items():
        diagnostic=item['diagnostic']
        if diagnostic is None or item['window']==0:continue
        if key in unreleased:
            if diagnostic['seen']!=0 or diagnostic['file_identity'] is not None:
                raise ValueError('Unreleased business contradicts an observed diagnostic file')
            # The finalizer may already have removed its temporary config
            # after joining cancellation. Exact non-release proves that this
            # payload could never open its diagnostic writer.
            observations[key]={'bytes':0,'file_identity':None,'newly_observed_bytes':0};continue
        folder=checked_path(Path(root),diagnostic['relative']);info=ordinary(folder)
        if not stat.S_ISDIR(info.st_mode) or [info.st_dev,info.st_ino]!=diagnostic['folder_identity']:
            raise ValueError('Stopped validator diagnostic folder changed')
        try:
            path=checked_path(Path(root),diagnostic['relative']+'/stderr.bin');info=ordinary(path)
        except FileNotFoundError:
            if diagnostic['file_identity'] is not None:raise ValueError('Stopped diagnostic file disappeared')
            observations[key]={'bytes':0,'file_identity':None,'newly_observed_bytes':0};continue
        pair=[info.st_dev,info.st_ino]
        if (not stat.S_ISREG(info.st_mode) or diagnostic['file_identity'] is not None and pair!=diagnostic['file_identity']
                or info.st_size<diagnostic['seen']):raise ValueError('Stopped validator diagnostic file changed')
        growth=info.st_size-diagnostic['seen']
        observations[key]={'bytes':info.st_size,'file_identity':pair,'newly_observed_bytes':growth}
    evidence={'progress':value,'diagnostic_observations':observations,
        'newly_retained_unknown_output_bytes':sum(v['window'] for v in value['channels'].values())}
    if unreleased:
        evidence['unreleased_channels']=unreleased
        evidence['newly_retained_unknown_output_bytes']=sum(item['window'] for key,item in value['channels'].items() if key not in unreleased)
    return validate_reconciliation(row,evidence,proofs),evidence


def validate_reconciliation(row,evidence,proofs=None):
    fields={'progress','diagnostic_observations','newly_retained_unknown_output_bytes'}
    if not isinstance(evidence,dict) or set(evidence) not in (fields,fields|{'unreleased_channels'}):
        raise ValueError('Missing independent output settlement evidence')
    value=validate(evidence['progress'],row);usage=copy.deepcopy(value['usage']);observations=evidence['diagnostic_observations']
    expected={k for k,v in value['channels'].items() if v['diagnostic'] is not None and v['window']}
    if not isinstance(observations,dict) or set(observations)!=expected:raise ValueError('Incomplete stopped diagnostic observations')
    from .finalizing_native_preparation import SCHEMA as PREPARATION,CANCEL_SCHEMA
    from .finalizing_native_scope import digest
    from .finalizing_linux_scope import unreleased as linux_unreleased
    unreleased={key:digest(proof) for key,proof in (proofs or {}).items() if proof.get('schema') in {PREPARATION,CANCEL_SCHEMA} or linux_unreleased(proof)}
    if evidence.get('unreleased_channels',{})!=unreleased or not set(unreleased)<=set(value['channels']):
        raise ValueError('Output credit release lacks the exact unstarted-business certificate')
    extra=0;uncertain=sum(v['window'] for key,v in value['channels'].items() if key not in unreleased)
    if evidence['newly_retained_unknown_output_bytes']!=uncertain:raise ValueError('Unknown pipe window was reset or fabricated')
    for key,item in observations.items():
        diagnostic=value['channels'][key]['diagnostic']
        if (not isinstance(item,dict) or set(item)!={'bytes','file_identity','newly_observed_bytes'}
                or type(item['bytes']) is not int or item['bytes']<diagnostic['seen']
                or item['newly_observed_bytes']!=item['bytes']-diagnostic['seen']
                or diagnostic['file_identity'] is not None and item['file_identity']!=diagnostic['file_identity']):
            raise ValueError('Stopped diagnostic growth differs from its last durable observation')
        extra+=item['newly_observed_bytes']
        if key in unreleased and (item['bytes'] or item['file_identity'] is not None or diagnostic['seen'] or diagnostic['file_identity'] is not None):
            raise ValueError('Diagnostic file contradicts its unstarted-business proof')
    if extra:usage['by_source']['diagnostic.finalizer']=usage['by_source'].get('diagnostic.finalizer',0)+extra
    usage['observed_output_bytes']+=extra;usage['observed_diagnostic_bytes']+=extra
    debt=usage.get('unsettled_output_bytes',0)+uncertain
    if debt:usage.update(schema='vibeflow.execution-output-usage.v3',unsettled_output_bytes=debt)
    if usage['observed_output_bytes']+debt>usage['max_log_bytes'] and usage['violation'] is None:
        usage.update(violation='log_limit',violation_source='native.finalizer')
    validate_usage(usage,usage['max_log_bytes'])
    return usage


async def initialize(db,owner,profile,budget):
    from ..models import NativeRequest
    from .job_output_finalizing import digest
    row=await db.get(NativeRequest,owner.id)
    value={'schema':SCHEMA,'profile_sha256':digest(profile),'sequence':1,'usage':budget.usage(),'channels':{}}
    row.result_json=json_text(validate(value,row))
