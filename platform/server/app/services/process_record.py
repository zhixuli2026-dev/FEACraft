"""Strict durable launch identity, bound to one attempt and its current owner."""
import re
import uuid
import json

from .process_identity_native import validate as validate_os
from .owned_process import SCHEMA

BASE={'schema','pid','runtime','launch_nonce','host','native','scope','gate_sha256','identity_probe_sha256'}
BINDING={'case_id','job_id','attempt_id','worker_id'}


def validate(record,*,bound=True):
    fields=BASE|BINDING if bound else BASE
    if not isinstance(record,dict) or set(record) not in (fields,fields|{'namespace_init'}) or record.get('schema')!=SCHEMA:
        raise ValueError('Invalid durable process identity schema')
    validate_os(record['host'])
    if type(record['pid']) is not int or record['pid']!=record['host']['pid'] or record['runtime'] not in {'windows','linux','wsl','wsl-sandbox'}:
        raise ValueError('Process host or runtime binding differs')
    for key in ('launch_nonce','gate_sha256','identity_probe_sha256'):
        if not isinstance(record[key],str) or not re.fullmatch(r'[0-9a-f]{64}',record[key]):
            raise ValueError('Invalid launch identity: '+key)
    scope='Local\\VibeFlow-'+record['launch_nonce'] if record['host']['platform']=='windows' else 'linux-pidfd'
    if record['scope']!=scope:
        raise ValueError('Process scope does not belong to the recorded launch')
    native=record['native']
    if record['runtime']=='linux' and (record['host']['platform']!='linux' or native is not None):
        raise ValueError('Linux host runtime requires a local Linux identity')
    if 'namespace_init' in record:
        init=validate_os(record['namespace_init']);host=record['host']
        if (host['platform']!='linux' or record['runtime'] not in {'windows','linux'} or native is not None
                or init['platform']!='linux' or any(init[key]!=host[key] for key in ('host_id','boot_id','uid'))
                or init['pid_namespace']==host['pid_namespace'] or init['pid']==host['pid']):
            raise ValueError('Process namespace init differs from its Linux host')
    if native is not None:
        if not isinstance(native,dict) or set(native)!={'distribution','process'} or record['host']['platform']!='windows':
            raise ValueError('Invalid native process binding')
        name=native['distribution']
        if not isinstance(name,str) or not name or len(name)>128 or any(ord(c)<32 for c in name):
            raise ValueError('Invalid native distribution binding')
        validate_os(native['process'])
        if native['process']['platform']!='linux':
            raise ValueError('WSL execution requires a Linux identity')
    if bound:
        if any(type(record[key]) is not int or record[key]<=0 for key in ('case_id','job_id')):
            raise ValueError('Invalid process case/job ownership')
        if not isinstance(record['attempt_id'],str) or str(uuid.UUID(record['attempt_id']))!=record['attempt_id']:
            raise ValueError('Invalid process attempt ownership')
        if not isinstance(record['worker_id'],str) or not record['worker_id'] or len(record['worker_id'])>128:
            raise ValueError('Invalid process worker ownership')
    return record


def bind(record,job,attempt,worker_id):
    validate(record,bound=False)
    bound={**record,'case_id':job.case_id,'job_id':job.id,'attempt_id':attempt.id,'worker_id':worker_id}
    return validate(bound)


def for_attempt(record,job,attempt):
    validate(record)
    if (record['case_id']!=job.case_id or record['job_id']!=job.id or record['attempt_id']!=attempt.id
            or record['worker_id']!=attempt.worker_id):
        raise ValueError('Process record crosses attempt ownership')
    return record


def public(job,attempt):
    """Expose only a fully bound identity; legacy or corrupt records stay opaque."""
    if attempt is None:
        return None
    try:
        return for_attempt(json.loads(attempt.process_identity_json),job,attempt)
    except (ValueError,TypeError,KeyError,AttributeError):
        return None


async def inspect(job,attempt):
    """Read-only observation, never a signal or permission to reuse a PID."""
    record=public(job,attempt)
    return await inspect_record(record)


async def inspect_record(record):
    """Observe an already bound, validated record, including a waiting launch."""
    import asyncio
    from . import process_identity_native as native_identity
    from .owned_process import probe_native
    if record is not None:validate(record)
    result={'identity':record,'state':'unverified','reason':'invalid_or_legacy_identity',
        'host':None,'native':None}
    if record is None:
        return result
    host=await asyncio.to_thread(native_identity.probe,record['host'])
    result.update(host=host,**host)
    if record['native'] is None:
        return result
    # A historical record from another Windows machine must never cause this
    # reader to launch a similarly named local distribution.
    if host['state'] in {'mismatch','unverified'}:
        return result
    native=await probe_native(record['native']['distribution'],record['native']['process'])
    result['native']=native
    if native['state']=='mismatch':
        result.update(state='mismatch',reason='native_birth_or_scope_changed')
    elif host['state']==native['state'] and host['state'] in {'match','exited'}:
        result.update(state=host['state'],reason='host_and_native_'+host['state'])
    else:
        result.update(state='unverified',reason='host_and_native_exit_not_agreed')
    return result
