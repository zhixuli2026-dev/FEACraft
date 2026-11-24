"""Bind the fixed Windows guardian to one durable image-cleanup child."""
import asyncio
import json
from pathlib import Path

from . import windows_scope_guardian as protocol, process_identity_native as identity


def selection(row, child, intent, value):
    if not isinstance(value, dict) or set(value) != {'request', 'preparation'}:
        raise ValueError('Image guardian selection is incomplete')
    request, prepared = value['request'], value['preparation']
    protocol.validate(request, check_code=False, check_directory=False)
    protocol.validate_prepared(request, prepared)
    expected = str(Path(json.loads(row.profile_json)['root']) / ('windows-' + child['id']))
    if (request['root']['path'] != expected or request['owner'] != json.loads(row.owner_identity_json)
            or request['nonce'] != intent['launch_nonce']):
        raise ValueError('Image guardian differs from its durable owner or evidence root')
    helpers = dict(intent['host_launch_sha256'], **{'process_identity_native.py': intent['identity_probe_sha256']})
    if any(helpers.get(name) != digest for name, digest in request['helpers'].items()):
        raise ValueError('Image guardian differs from the selected launch implementation')
    return value


def validate_proof(selected, value):
    if (not isinstance(value, dict) or set(value) != {'request', 'preparation', 'receipt', 'guardian_exit'}
            or value['request'] != selected['request'] or value['preparation'] != selected['preparation']):
        raise ValueError('Image guardian proof differs from admission')
    protocol.validate_receipt(value['request'], value['preparation'], value['receipt'], check_directory=False)
    observed = value['guardian_exit']
    if (not isinstance(observed, dict) or set(observed) != {'state', 'reason'} or observed['state'] != 'exited'
            or observed['reason'] not in {'retained_creation_handle', 'not_found', 'process_exited'}):
        raise ValueError('Image guardian has no actual exit observation')
    return value


def native_selection(value,intent,distribution):
    from . import native_process_preflight as preflight
    if (not isinstance(value,dict) or set(value)!={'schema','launch_nonce','host','native','helpers'}
            or value['schema']!=preflight.SCHEMA or value['launch_nonce']!=intent['launch_nonce']):
        raise ValueError('Invalid durable native preflight')
    if identity.validate(value['host'])['platform']!='windows':
        raise ValueError('Native preflight lacks a Windows host birth')
    native=value['native']
    if (not isinstance(native,dict) or set(native)!={'distribution','process'}
            or native['distribution']!=distribution or identity.validate(native['process'])['platform']!='linux'
            or native['process']['uid']!=0):
        raise ValueError('Native preflight changed its distribution or birth')
    expected={'native_process_preflight.py':intent['host_launch_sha256'].get('native_process_preflight.py'),
              'process_gate.py':intent['gate_sha256'],'process_identity_native.py':intent['identity_probe_sha256'],
              'process_cgroup_native.py':intent['native_scope_helpers']['process_cgroup_native.py']}
    if expected['native_process_preflight.py'] is None or value['helpers']!=expected:
        raise ValueError('Native preflight implementation differs from durable intent')
    return value


async def recover(selected):
    """Read-only observation; never open a named Job or terminate a looked-up PID."""
    request, prepared = selected['request'], selected['preparation']
    async with asyncio.timeout(15):
        while True:
            observed = await asyncio.to_thread(identity.probe, prepared['guardian'])
            if observed['state'] == 'exited':
                break
            if observed['state'] != 'match':
                raise ValueError('Image guardian exit cannot be verified')
            await asyncio.sleep(.05)
    durable_prepared = await asyncio.to_thread(protocol.read, request, 'prepared.json')
    if durable_prepared != prepared:
        raise ValueError('Image guardian durable preparation changed')
    receipt = await asyncio.to_thread(protocol.read, request, 'receipt.json')
    protocol.validate_receipt(request, prepared, receipt)
    return validate_proof(selected, dict(selected, receipt=receipt, guardian_exit=observed))
