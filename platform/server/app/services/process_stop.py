"""Bounded process cleanup evidence; distinct from a cancellation request."""
from datetime import datetime,timedelta
import json
import re

REASONS={'cancelled','shutdown','timeout','log_limit','disk_limit','disk_unverified','storage_limit',
    'cpu_limit','memory_limit','compute_unverified','ownership_lost','executor_error','root_exit','finalization_error'}
FIELDS={'schema','launch_nonce','reason','grace_ms','request_sent','forced','scope_empty','native_state',
    'root_exit_code','control_valid','control','verified','error','started_at','completed_at'}
CONTROL={'schema','nonce','role','stop_received','grace_forwarded','forced','exit_code'}


def validate(value,identity):
    if not isinstance(value,dict) or set(value) not in (FIELDS,FIELDS|{'scope_observation'}) or value['schema']!='vibeflow.process-stop.v1':
        raise ValueError('Invalid process cleanup evidence')
    if value['launch_nonce']!=identity['launch_nonce'] or value['reason'] not in REASONS:
        raise ValueError('Cleanup belongs to another launch')
    if type(value['grace_ms']) is not int or not 0<=value['grace_ms']<=10000:
        raise ValueError('Invalid process cleanup grace')
    for key in ('request_sent','forced','scope_empty','control_valid','verified'):
        if type(value[key]) is not bool: raise ValueError('Invalid cleanup flag')
    if value['native_state'] not in {None,'match','exited','mismatch','unverified'}:
        raise ValueError('Invalid native cleanup state')
    if value['root_exit_code'] is not None and type(value['root_exit_code']) is not int:
        raise ValueError('Invalid root exit code')
    if value['error'] is not None and (not isinstance(value['error'],str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',value['error'])):
        raise ValueError('Invalid cleanup error category')
    for key in ('started_at','completed_at'):
        stamp=datetime.fromisoformat(value[key])
        if stamp.utcoffset()!=timedelta(0): raise ValueError('Cleanup timestamps require UTC')
    if 'scope_observation' in value:
        observation=value['scope_observation']
        if identity['host']['platform']=='windows':
            fields={'schema','total','observed','exit_verified','gone_before_open','pending','complete','error'}
            if (not isinstance(observation,dict) or set(observation)!=fields
                    or observation['schema']!='vibeflow.windows-members.v1'
                    or type(observation['complete']) is not bool):
                raise ValueError('Invalid Job membership evidence')
            for key in ('total','observed','exit_verified','gone_before_open','pending'):
                number=observation[key]
                if key=='total' and number is None: continue
                if type(number) is not int or not 0<=number<=65536:
                    raise ValueError('Invalid Job membership count')
            error=observation['error']
            if error is not None and (not isinstance(error,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',error)):
                raise ValueError('Invalid Job membership error')
            if observation['observed']!=observation['exit_verified']+observation['gone_before_open']+observation['pending']:
                raise ValueError('Inconsistent Job membership evidence')
            if observation['complete'] and (observation['total']!=observation['observed'] or observation['pending'] or error):
                raise ValueError('Job membership completion lacks exit proof')
            if value['verified'] and not observation['complete']:
                raise ValueError('Cleanup lacks complete Job membership evidence')
        elif observation is not None:
            raise ValueError('Unexpected Windows membership evidence')
    rows=value['control']
    if not isinstance(rows,list) or len(rows)>2: raise ValueError('Invalid cleanup control records')
    roles=set()
    allowed={'wsl','init'} if identity['native'] else {'host'} if identity['host']['platform']=='windows' else {'host','init'}
    for row in rows:
        if (not isinstance(row,dict) or set(row)!=CONTROL or row['schema']!='vibeflow.gate-exit.v1'
                or row['nonce']!=identity['launch_nonce'] or row['role'] not in allowed or row['role'] in roles
                or any(type(row[k]) is not bool for k in ('stop_received','grace_forwarded','forced'))
                or type(row['exit_code']) is not int):
            raise ValueError('Invalid cleanup control record')
        roles.add(row['role'])
    if value['verified'] and (not value['scope_empty'] or value['root_exit_code'] is None or value['error'] is not None
            or (identity['native'] is not None and value['native_state']!='exited')
            or (value['root_exit_code']==0 and (not value['control_valid'] or roles!=allowed))):
        raise ValueError('Cleanup verification lacks exit proof')
    return value


def public(job,attempt):
    from .process_record import public as process_identity
    identity=process_identity(job,attempt)
    if identity is None: return None
    try:
        value=json.loads(attempt.output_manifest_json).get('process_stop')
        return validate(value,identity)
    except (ValueError,TypeError,KeyError,AttributeError):
        return None
