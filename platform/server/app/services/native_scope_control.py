"""Owned Windows bootstrap for the fixed native scope controller."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys

from . import native_scope_controller as protocol
from . import process_cgroup_native as cgroups, process_identity_native as identity
from .async_ops import finish_before_cancel

CONTROLLER = Path(protocol.__file__).resolve()


class ScopeUnavailable(ValueError):
    pass


def validate_receipt(value):
    """Validate a bounded durable receipt, without performing any native action."""
    from . import process_record, process_stop
    fields = {'request', 'request_sha256', 'controller',
              'controller_exit', 'membership', 'host', 'host_stop', 'result', 'error'}
    if (not isinstance(value, dict) or set(value) not in (fields, fields | {'preparation'})
            or len(json.dumps(value).encode()) > 32768 or value['error'] is not None):
        raise ValueError('Invalid successful native scope receipt')
    request = protocol.validate(value['request'], check_code=False)
    if ('preparation' in value) != (request['operation'] == 'allocate'):
        raise ValueError('Only an allocation receipt carries prepared creator evidence')
    if value['request_sha256'] != protocol.digest(request):
        raise ValueError('Native scope receipt request changed')
    host = process_record.validate(value['host'], bound=False)
    stop = process_stop.validate(value['host_stop'], host)
    if (host['runtime'] != 'windows' or host['native'] is not None or
            host['launch_nonce'] != request['nonce'] or not stop['verified']):
        raise ValueError('Native scope controller host differs or did not stop')
    birth = identity.validate(value['controller'])
    if birth['platform'] != 'linux' or birth['uid'] != 0 or value['controller_exit'] != {
            'state': 'exited', 'reason': 'process_exited'}:
        raise ValueError('Native scope controller exit is unverified')
    operation = request['operation']
    result = value['result']
    if operation == 'allocate':
        prepared = validate_prepared(value.get('preparation'))
        if (prepared['request'] != request or prepared['host'] != host
                or prepared['native']['controller'] != birth or prepared['native']['membership'] != value['membership']):
            raise ValueError('Allocation receipt differs from its persisted preparation')
        selected = cgroups.validate(result)
        reservation = prepared['native']['reservation']
        if selected['schema'] != cgroups.ALLOCATION_SCHEMA or any(
                selected[key] != reservation[key] for key in ('nonce', 'boot_id', 'device', 'parent_inode')):
            raise ValueError('Allocated scope differs from its reservation')
        policy=request['arguments'].get('compute_policy')
        if policy is not None:
            from .native_compute_v1 import validate as compute_scope
            if compute_scope(selected.get('compute'))['policy']!=policy:
                raise ValueError('Allocated compute scope differs from its frozen policy')
        elif 'compute' in selected:
            raise ValueError('Native compute scope was not requested')
    elif operation == 'abandon':
        reservation = request['arguments']['preparation']['reservation']
        if (not isinstance(result, dict) or set(result) != {
                'reservation', 'resolved_scope', 'state', 'removed_this_call', 'creator_exit'}
                or result['reservation'] != reservation or result['state'] != 'absent'
                or type(result['removed_this_call']) is not bool or result['creator_exit'] != {
                    'state': 'exited', 'reason': 'process_exited'}):
            raise ValueError('Unadmitted allocation retirement is unverified')
        resolved = result['resolved_scope']
        if result['removed_this_call'] != (resolved is not None):
            raise ValueError('Allocation removal must identify the actual removed scope')
        if resolved is not None:
            cgroups.validate(resolved)
            if resolved['schema'] != cgroups.ALLOCATION_SCHEMA or any(
                    resolved[key] != reservation[key] for key in ('nonce', 'boot_id', 'device', 'parent_inode')):
                raise ValueError('Removed scope differs from its reservation')
        # No inode is invented when the allocation is absent: only the reserved
        # boot and physical membership path are used below.
        selected = reservation
    elif operation == 'create':
        selected = cgroups.validate(result)
        if selected['nonce'] != request['arguments']['launch_nonce'] or selected['schema'] != cgroups.SCHEMA:
            raise ValueError('Created native scope belongs to another launch')
    else:
        keys = {'scope', 'state'} | ({'removed_this_call'} if operation == 'retire' else set())
        compute = request['arguments']['scope'].get('compute')
        if compute is not None:
            keys.add('compute_usage')
        if (not isinstance(result, dict) or set(result) != keys or result['scope'] != request['arguments']['scope']
                or result['state'] not in {'empty', 'populated', 'absent'}):
            raise ValueError('Native scope receipt differs from its selection')
        if operation == 'retire' and (result['state'] != 'absent' or type(result['removed_this_call']) is not bool):
            raise ValueError('Native scope retirement is unverified')
        selected = cgroups.validate(result['scope'])
        if compute is not None:
            from .native_compute_v1 import validate_usage
            validate_usage(compute,result['compute_usage'],allow_absent=operation=='retire')
    if selected['boot_id'] != birth['boot_id']:
        raise ValueError('Controller and selected scope boot differ')
    membership = value['membership']
    if not isinstance(membership, str) or len(membership.encode()) > 4096:
        raise ValueError('Invalid scope controller membership')
    paths = [line[3:] for line in membership.splitlines() if line.startswith('0::')]
    target = ('/' + cgroups.PARENT + '/a-' + selected['nonce'] if operation == 'abandon'
              else cgroups.membership_path(selected))
    if len(paths) != 1 or not paths[0].startswith('/') or paths[0] == target or paths[0].startswith(target + '/'):
        raise ValueError('Native scope controller was not outside its target')
    return value


def validate_prepared(value):
    from . import process_record
    if (not isinstance(value, dict) or set(value) != {'request', 'native', 'host'}
            or len(json.dumps(value).encode()) > 32768):
        raise ValueError('Invalid durable allocation preparation')
    request = protocol.validate(value['request'], check_code=False)
    protocol.validate_preparation(request, value['native'])
    host = process_record.validate(value['host'], bound=False)
    if host['runtime'] != 'windows' or host['native'] is not None or host['launch_nonce'] != request['nonce']:
        raise ValueError('Prepared allocation host differs from its request')
    return value


class ScopeControl:
    def __init__(self, distribution, operation, arguments, *, on_prepared=None, check_active=None):
        from .owned_process import OwnedProcess
        self.owned = OwnedProcess()
        self.request = {'schema': protocol.SCHEMA, 'nonce': self.owned.nonce,
            'distribution': distribution, 'operation': operation, 'arguments': copy.deepcopy(arguments),
            'helpers': {name: hashlib.sha256(CONTROLLER.with_name(name).read_bytes()).hexdigest() for name in protocol.FILES}}
        protocol.validate(self.request)
        self.receipt = None
        self.preparation = None
        self.on_prepared = on_prepared
        self.check_active = check_active
        if operation == 'allocate' and on_prepared is None:
            raise ValueError('Allocation requires a durable preparation callback')

    async def run(self):
        from .control_identity import read
        from .control_identity_reader import bounded
        owned = self.owned
        reason = 'executor_error'
        try:
            if self.check_active is not None:self.check_active()
            protocol.validate(self.request)
            encoded = base64.b64encode(json.dumps(self.request, separators=(',', ':')).encode()).decode('ascii')
            await owned.prepare([str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                '-I', '-S', '-u', str(CONTROLLER), '--host', encoded],
                cwd=CONTROLLER.parent, env=None, runtime='windows',
                terminal_io=self.request['operation'] == 'allocate')
            protocol.validate(self.request)
            await owned.verify_registration()
            if self.check_active is not None:self.check_active()
            await owned.release()
            async def receive():
                if self.request['operation'] == 'allocate':
                    raw = await owned.proc.stdout.readline()
                    if len(raw) > protocol.LIMIT or not raw.endswith(b'\n'):
                        raise ScopeUnavailable('Invalid allocation preparation frame')
                    frame = json.loads(raw)
                    if frame.get('stage') == 'prepared':
                        protocol.validate_preparation(self.request, frame)
                        observed = await read(self.request['distribution'], 'identity', {'expected': frame['controller']})
                        if observed['state'] != 'match':
                            raise ScopeUnavailable('Prepared native controller is not live')
                        self.preparation = validate_prepared({'request': copy.deepcopy(self.request),
                            'native': frame, 'host': copy.deepcopy(owned.record)})
                        await self.on_prepared(copy.deepcopy(self.preparation))
                        protocol.validate(self.request)
                        await owned.verify_registration()
                        if self.check_active is not None:self.check_active()
                        data = (json.dumps(protocol.permit(self.request, frame)) + '\n').encode()
                        envelope = {'action': 'terminal_io', 'nonce': owned.nonce,
                                    'data': base64.b64encode(data).decode('ascii')}
                        owned.proc.stdin.write((json.dumps(envelope) + '\n').encode())
                        await owned.proc.stdin.drain()
                        data = await bounded(owned.proc.stdout, protocol.LIMIT)
                    else:
                        data = raw + await bounded(owned.proc.stdout, protocol.LIMIT - len(raw))
                else:
                    data = await bounded(owned.proc.stdout, protocol.LIMIT)
                code = await owned.wait_root_exit()
                if code:
                    raise ScopeUnavailable('Scope controller exited without a valid response')
                return json.loads(data)
            value = await asyncio.wait_for(receive(), 70 if self.request['operation'] == 'allocate' else 35)
            if (not isinstance(value, dict) or set(value) != {
                    'schema', 'request_sha256', 'controller', 'membership', 'result', 'error'}
                    or value['schema'] != protocol.SCHEMA or value['request_sha256'] != protocol.digest(self.request)):
                raise ScopeUnavailable('Scope controller response binding differs')
            await finish_before_cancel(owned.finish())
            birth = value['controller']
            if birth is None:
                if value['result'] is not None or value['error'] not in {
                        'distribution_not_running', 'distribution_list_unavailable', 'native_controller_failed'}:
                    raise ScopeUnavailable('Invalid unavailable scope controller response')
                raise ScopeUnavailable(value['error'])
            if identity.validate(birth)['platform'] != 'linux':
                raise ScopeUnavailable('Scope controller birth is not native')
            exited = await read(self.request['distribution'], 'identity', {'expected': birth})
            if exited['state'] != 'exited':
                raise ScopeUnavailable('Native scope controller exit is unverified')
            membership = value['membership']
            if not isinstance(membership, str) or len(membership.encode()) > 4096:
                raise ScopeUnavailable('Invalid scope controller membership')
            self.receipt = {'request': copy.deepcopy(self.request), 'request_sha256': protocol.digest(self.request),
                'controller': birth, 'controller_exit': exited, 'membership': membership,
                'host': copy.deepcopy(owned.record), 'host_stop': copy.deepcopy(owned.stop_result),
                'result': value['result'], 'error': value['error']}
            if self.preparation is not None:
                self.receipt['preparation'] = copy.deepcopy(self.preparation)
            if value['error'] is not None:
                raise ScopeUnavailable('Native scope operation failed: ' + str(value['error'])[:80])
            validate_receipt(self.receipt)
            return copy.deepcopy(self.receipt)
        except asyncio.CancelledError:
            reason = 'cancelled'; raise
        except asyncio.TimeoutError as error:
            reason = 'timeout'
            raise ScopeUnavailable('Native scope controller exceeded its deadline') from error
        finally:
            try:
                if owned.proc is not None and not owned.closed and owned.stop_result is None:
                    await finish_before_cancel(owned.stop(reason=reason, grace_ms=0))
            finally:
                owned.close()


async def control(distribution, operation, arguments, *, on_prepared=None):
    return await ScopeControl(distribution, operation, arguments, on_prepared=on_prepared).run()
