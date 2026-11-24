"""Bind managed CLI scope admission and actual closure to its durable session."""
import asyncio,copy,hashlib,json,re
from pathlib import Path

from . import native_scope_control as scopes,native_scope_controller as controller
from . import native_process_preflight as preflight,windows_scope_guardian as guardian
from . import process_identity_native as identity,cli_native_handoff as handoff
from .async_ops import finish_before_cancel
from .durable_events import json_text

SCHEMA='vibeflow.cli-scope-lifecycle.v1'
MAX_SECONDS=3600
FILES=tuple(sorted(set(controller.FILES+guardian.FILES+preflight.FILES)))
HERE=Path(__file__).resolve().parent


def selection(workspace):
    return {'schema':SCHEMA,'guardian_root':str(Path(workspace).parent/'windows'),
        'max_seconds':MAX_SECONDS,'helpers':{name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in FILES}}


def validate_profile(selected):
    lifecycle=selected['scope_lifecycle'];managed=selected['native_management']
    from .process_cgroup_native import validate
    if (not isinstance(managed,dict) or set(managed)!={'schema','scope','helpers'}
            or managed['schema']!=handoff.SCHEMA or set(managed['helpers'])!=set(handoff.FILES)
            or validate(managed['scope'])['nonce']!=selected['nonce']):
        raise ValueError('Invalid managed CLI launch binding')
    if (not isinstance(lifecycle,dict) or set(lifecycle)!={'schema','guardian_root','max_seconds','helpers'}
            or lifecycle['schema']!=SCHEMA or lifecycle['max_seconds']!=MAX_SECONDS
            or not isinstance(lifecycle['guardian_root'],str) or len(lifecycle['guardian_root'])>4096
            or not Path(lifecycle['guardian_root']).is_absolute() or '\0' in lifecycle['guardian_root']
            or set(lifecycle['helpers'])!=set(FILES)):
        raise ValueError('Invalid CLI scope lifecycle profile')
    for hashes in (managed['helpers'],lifecycle['helpers']):
        if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in hashes.values()):
            raise ValueError('Invalid CLI scope implementation digest')
    for name in set(managed['helpers'])&set(lifecycle['helpers']):
        if managed['helpers'][name]!=lifecycle['helpers'][name]:raise ValueError('CLI scope helpers differ')
    for name,key in (('stdio_bridge.py','bridge_sha256'),('process_identity_native.py','identity_sha256'),('codex_wsl_state.py','native_state_sha256')):
        if managed['helpers'][name]!=selected['transport'][key]:raise ValueError('CLI handoff differs from its transport')
    return lifecycle


def check_code(selected):
    lifecycle=validate_profile(selected)
    for hashes in (lifecycle['helpers'],selected['native_management']['helpers']):
        if any(hashlib.sha256((HERE/name).read_bytes()).hexdigest()!=v for name,v in hashes.items()):
            raise ValueError('Managed CLI implementation changed after admission')


def guardian_selection(row,selected,value):
    if not isinstance(value,dict) or set(value)!={'request','preparation'}:raise ValueError('Incomplete CLI guardian')
    request=value['request'];guardian.validate(request,check_code=False,check_directory=False)
    guardian.validate_prepared(request,value['preparation']);frozen=selected['scope_lifecycle']
    if (request['owner']!=json.loads(row.owner_identity_json) or request['nonce']!=row.launch_nonce
            or request['root']['path']!=frozen['guardian_root'] or request['max_seconds']!=frozen['max_seconds']
            or request['helpers']!={name:frozen['helpers'][name] for name in guardian.FILES}):
        raise ValueError('CLI guardian differs from its frozen session')
    return value


def validate(row,selected,journal):
    frozen=validate_profile(selected);value=journal.get('management')
    fields={'allocation','guardian','preflight','retirement'}
    if not isinstance(value,dict) or set(value) not in (fields,fields|{'recovery'}):
        raise ValueError('Managed CLI lacks its scope journal')
    allocated=scopes.validate_receipt(value['allocation']);request=allocated['request']
    if (request['operation']!='allocate' or allocated['result']!=selected['native_management']['scope']
            or request['distribution']!=selected['distro']
            or request['helpers']!={name:frozen['helpers'][name] for name in controller.FILES}):
        raise ValueError('CLI scope differs from its durable allocation')
    if value['guardian'] is not None:guardian_selection(row,selected,value['guardian'])
    admission=value['preflight']
    if admission is not None:
        if (value['guardian'] is None or not isinstance(admission,dict)
                or set(admission)!={'schema','launch_nonce','host','native','helpers'}
                or admission['schema']!=preflight.SCHEMA or admission['launch_nonce']!=row.launch_nonce
                or admission['helpers']!={name:frozen['helpers'][name] for name in preflight.FILES}
                or identity.validate(admission['host'])['platform']!='windows'):
            raise ValueError('CLI native preflight lacks its durable guardian')
        native=admission['native']
        if (set(native)!={'distribution','process'} or native['distribution']!=selected['distro']
                or identity.validate(native['process'])['platform']!='linux' or native['process']['uid']!=0):
            raise ValueError('CLI preflight differs from its native management identity')
    root=journal['root'];bridge=journal['bridge']
    if root is not None:
        if (admission is None or root['host']!=admission['host'] or root['native']!=admission['native']
                or root['gate_sha256']!=frozen['helpers']['process_gate.py']
                or root['identity_probe_sha256']!=frozen['helpers']['process_identity_native.py']):
            raise ValueError('CLI root differs from its admitted native birth')
    proof=value['retirement']
    if proof is not None:
        if not isinstance(proof,dict) or set(proof)!={'scope','guardian','births','exits'}:
            raise ValueError('Invalid managed CLI retirement')
        receipt=scopes.validate_receipt(proof['scope']);request=receipt['request']
        if (request['operation']!='retire' or request['arguments']!={'scope':allocated['result']}
                or request['distribution']!=selected['distro']
                or request['helpers']!={name:frozen['helpers'][name] for name in controller.FILES}):
            raise ValueError('CLI scope retirement changed its selected allocation')
        if value['guardian'] is None:
            if proof['guardian'] is not None:raise ValueError('Unregistered CLI guardian receipt')
        else:
            from .sandbox_image_guardian import validate_proof
            validate_proof(value['guardian'],proof['guardian'])
        births=proof['births'];exits=proof['exits']
        if not isinstance(births,dict) or not set(births)<={'host','native','bridge'} or set(exits)!=set(births):
            raise ValueError('Invalid CLI retirement observations')
        expected={} if admission is None else {'host':admission['host'],'native':admission['native']['process']}
        if bridge is not None:expected['bridge']=bridge
        if any(births.get(k)!=v for k,v in expected.items()):raise ValueError('CLI retirement lacks a registered process')
        for role,birth in births.items():
            identity.validate(birth)
            if exits[role]!={'state':'exited','reason':'process_exited'}:raise ValueError('CLI process exit is unverified')
            if role=='bridge' and (admission is None or birth['platform']!='linux' or birth['uid']!=65534
                    or any(birth[k]!=admission['native']['process'][k] for k in ('host_id','boot_id'))):
                raise ValueError('CLI retirement bridge differs from its native management')
    if row.phase=='closed' and proof is None:raise ValueError('Closed managed CLI lacks full scope retirement')
    if 'recovery' in value:
        from .cli_session_recovery import validate as validate_recovery
        validate_recovery(row,journal)
    return value


async def register(db,owner,field,value):
    from . import codex_records as records
    if field not in {'guardian','preflight'}:raise ValueError('Invalid scope admission stage')
    row=await records.current(db,owner,launch=True);journal,_=records.document(row)
    if journal['management'][field] is not None or journal['root'] is not None:
        raise ValueError('CLI scope admission stage already registered')
    journal['management'][field]=copy.deepcopy(value);row.process_json=json_text(journal);records.document(row)


async def close(session):
    """Called only after the retained client's close has drained its writers."""
    from ..db import SessionLocal
    from ..models import NativeCliSession
    from . import codex_records as records
    from .owned_process import probe_native
    from .sandbox_image_guardian import recover
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,session.id)
        journal,_=records.document(row);value=copy.deepcopy(journal['management'])
    check_code(session.selected)
    owned=session.client.transport.owned
    # Retain actual startup evidence even if a callback commit was interrupted.
    if owned.guardian is not None and owned.guardian.prepared is not None:
        candidate={'request':owned.guardian.request,'preparation':owned.guardian.prepared}
        if value['guardian'] is not None and value['guardian']!=candidate:raise ValueError('CLI guardian changed during cleanup')
        value['guardian']=copy.deepcopy(candidate)
    if owned.native_preflight is not None:
        if value['preflight'] is not None and value['preflight']!=owned.native_preflight:raise ValueError('CLI preflight changed during cleanup')
        value['preflight']=copy.deepcopy(owned.native_preflight)
    guardian_proof=None
    if value['guardian'] is not None:guardian_proof=owned.guardian_proof or await recover(value['guardian'])
    receipt=await scopes.control(session.distro,'retire',{'scope':session.selected['native_management']['scope']})
    if owned.proc is not None and value['preflight'] is None:
        # An absent business cgroup does not identify a native bootstrap that
        # may have been created before its preflight frame was observed.
        raise ValueError('Native CLI bootstrap birth is unverified; scope absence alone is insufficient')
    births={};admission=value['preflight']
    if admission is not None:births.update(host=admission['host'],native=admission['native']['process'])
    bridge=session.client.transport.bridge_birth
    if bridge is not None:births['bridge']=bridge
    exits={}
    for role,birth in births.items():
        exits[role]=(await finish_before_cancel(asyncio.to_thread(identity.probe,birth)) if role=='host' else await probe_native(session.distro,birth))
    value['retirement']={'scope':receipt,'guardian':guardian_proof,'births':births,'exits':exits}
    final=dict(journal,management=value,root=owned.record or journal['root'])
    validate(row,session.selected,final)
    return value
