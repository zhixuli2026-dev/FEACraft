"""Partial startup observations are evidence, never complete launch/stop authority."""
import asyncio
from datetime import datetime,timedelta
import re

from . import process_identity_native as identity

SCHEMA='vibeflow.process-preparation.v1'
FIELDS={'schema','stage','launch_nonce','runtime','host','native','scope','gate_sha256','identity_probe_sha256','observed_at'}
STAGES=('created','host_ready','native_ready')


def validate(value):
    if not isinstance(value,dict) or set(value)!=FIELDS or value['schema']!=SCHEMA or value['stage'] not in STAGES:
        raise ValueError('Invalid partial process preparation')
    if value['runtime'] not in {'windows','linux','wsl','wsl-sandbox'}:raise ValueError('Invalid preparation runtime')
    for key in ('launch_nonce','gate_sha256','identity_probe_sha256'):
        if not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{64}',value[key]):raise ValueError('Invalid preparation binding')
    identity.validate(value['host'])
    if value['runtime']=='linux' and value['host']['platform']!='linux':raise ValueError('Linux preparation requires its local host')
    expected='Local\\VibeFlow-'+value['launch_nonce'] if value['host']['platform']=='windows' else 'linux-pidfd'
    if value['scope']!=(None if value['stage']=='created' else expected):raise ValueError('Preparation scope differs')
    native=value['native']
    if value['stage']=='native_ready':
        if (value['runtime'] not in {'wsl','wsl-sandbox'} or not isinstance(native,dict) or set(native)!={'distribution','process'} or value['host']['platform']!='windows'
                or not isinstance(native['distribution'],str) or not native['distribution'] or len(native['distribution'])>128
                or any(ord(c)<32 for c in native['distribution'])):raise ValueError('Invalid native preparation')
        identity.validate(native['process'])
        if native['process']['platform']!='linux':raise ValueError('Native preparation requires Linux identity')
    elif native is not None:raise ValueError('Partial preparation invents native readiness')
    stamp=value['observed_at']
    if not isinstance(stamp,str) or len(stamp)>64 or datetime.fromisoformat(stamp).utcoffset()!=timedelta(0):
        raise ValueError('Preparation observation requires UTC')
    return value


def chain(values,intent):
    if len(values)>3:raise ValueError('Too many preparation observations')
    for index,value in enumerate(values):
        validate(value)
        if value['stage']!=STAGES[index]:raise ValueError('Preparation observations are missing or reordered')
        if intent is None or any(value[key]!=intent[key] for key in ('launch_nonce','runtime','gate_sha256','identity_probe_sha256')):
            raise ValueError('Preparation observation differs from launch intent')
        if index and value['host']!=values[0]['host']:raise ValueError('Preparation changed the created host')
        if value['native'] is not None and 'distribution' in intent and value['native']['distribution']!=intent['distribution']:
            raise ValueError('Preparation changed the intended native distribution')
    return values[-1] if values else None


def matches_record(value,record):
    validate(value)
    expected='native_ready' if record['native'] is not None else 'host_ready'
    if value['stage']!=expected or any(value[key]!=record[key] for key in (
            'launch_nonce','runtime','host','native','scope','gate_sha256','identity_probe_sha256')):
        raise ValueError('Completed identity differs from observed preparation')


async def inspect(value):
    validate(value)
    from .owned_process import probe_native
    from .async_ops import finish_before_cancel
    host=await finish_before_cancel(asyncio.to_thread(identity.probe,value['host']))
    result={'stage':value['stage'],'host':host,'native':None,'state':host['state'],'reason':host['reason']}
    if host['state'] in {'mismatch','unverified'}:return result
    if value['native'] is not None:
        native=await probe_native(value['native']['distribution'],value['native']['process']);result['native']=native
        if host['state']==native['state'] and host['state'] in {'match','exited'}:
            result.update(state=host['state'],reason='host_and_native_'+host['state'])
        else:result.update(state='unverified',reason='preparation_members_do_not_agree')
    elif value['runtime'] in {'wsl','wsl-sandbox'}:
        result.update(state='unverified',reason='native_preparation_not_recorded')
    return result
