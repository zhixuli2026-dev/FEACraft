"""Read-only process evidence for persistent terminal recovery.

This is not a recovery grant: mounting, a new durable operation, exclusive image
ownership and publication preconditions must be checked by the eventual executor.
No lease timestamp, missing local session or old cleanup receipt proves exit.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import json

from sqlalchemy import JSON, case, cast, func, or_, select

from .. import storage
from ..models import NativeRequest
from . import native_request_records, process_identity_native as identity, terminal_records
from .durable_events import now
from .owned_process import probe_native, wsl_path

MAX_EXPORTS = 8
MAX_CHILDREN = 8
MAX_JOURNAL = 512 * 1024
_slots = asyncio.Semaphore(2)


class InspectionBlocked(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Snapshot:
    session_id: str
    fingerprint: str
    owners: tuple
    processes: tuple


def result(sid, *, reason, status='blocked', checks=None):
    return {'session_id': sid, 'status': status, 'reason': reason,
            'observed_at': now().isoformat(), 'checks': checks or [],
            'recovery_performed': False, 'command_replayed': False,
            'image_verified': False}


def bounded(raw):
    if not isinstance(raw, str) or len(raw.encode('utf8')) > MAX_JOURNAL:
        raise InspectionBlocked('journal_exceeds_bound')
    return json.loads(raw)


async def capture(db, row, case_prefix):
    """Capture committed ownership, with bounded matching-export selection.

    Caller supplies an authorized, available case. Include profiles missing their
    session binding: silently skipping such a row could hide an unknown writer.
    Invalid PostgreSQL JSON rejects the inspection rather than hiding the row.
    """
    if row.shell != 'wsl-sandbox':
        raise InspectionBlocked('persistent_sandbox_required')
    selected = bounded(row.profile_json)
    bounded(row.process_json); bounded(row.stop_json)
    owner_birth = bounded(row.owner_identity_json)
    journal, _ = terminal_records.document(row)
    command = selected['terminal']['command']
    if '--persistent-export' not in command:
        raise InspectionBlocked('persistent_sandbox_required')
    if selected['source_prefix'] != case_prefix:
        raise InspectionBlocked('case_source_changed')
    if journal['root'] is None or journal['gate'] is None:
        raise InspectionBlocked('terminal_process_registration_incomplete')
    root = storage._fs_root().resolve() / '.terminal-export' / command[-1].rsplit('/', 1)[-1]
    if wsl_path(root) != command[-1]:
        raise InspectionBlocked('image_storage_binding_changed')

    def field(name):
        if db.bind.dialect.name == 'sqlite':
            return case((func.json_valid(NativeRequest.profile_json),
                         func.json_extract(NativeRequest.profile_json, '$.' + name)), else_=None)
        if db.bind.dialect.name == 'postgresql':
            return cast(NativeRequest.profile_json, JSON)[name].as_string()
        raise InspectionBlocked('database_not_supported')

    binding, image_binding = field('session_id'), field('root')
    exports = list(await db.scalars(select(NativeRequest).where(
        NativeRequest.case_id == row.case_id, NativeRequest.kind == 'terminal.export',
        or_(binding == row.id, image_binding == str(root), binding.is_(None), image_binding.is_(None)))
        .order_by(NativeRequest.id).limit(MAX_EXPORTS + 1)))
    if len(exports) > MAX_EXPORTS:
        raise InspectionBlocked('export_history_exceeds_inspection_bound')
    owners = [('terminal_owner', owner_birth, row.phase in {'starting', 'active'})]
    processes = []

    def add_process(role, process):
        if process is None or process['native'] is None:
            raise InspectionBlocked('process_registration_incomplete')
        processes.append((role + '_host', None, process['host']))
        processes.append((role + '_native', process['native']['distribution'], process['native']['process']))

    add_process('terminal', journal['root'])
    processes.append(('terminal_gate', selected['distro'], journal['gate']['process']))
    # Hash immutable copies of every relevant column, not merely a lease/version.
    documents = [(row.id, row.case_id, row.created_by, row.phase, row.version,
                  row.owner_identity_json, row.launch_nonce, row.profile_json,
                  row.process_json, row.stop_json, row.error)]
    for export in exports:
        profile = bounded(export.profile_json)
        bounded(export.children_json)
        birth = bounded(export.owner_identity_json)
        if (export.created_by != row.created_by or profile.get('session_id') != row.id
                or profile.get('terminal_process') != journal['root']
                or profile.get('gate') != journal['gate']['process']
                or profile.get('distro') != selected['distro']
                or profile.get('root') != str(root) or profile.get('filesystem') != 'ext4'):
            raise InspectionBlocked('export_binding_invalid')
        children = native_request_records.document(export)['children']
        if len(children) > MAX_CHILDREN:
            raise InspectionBlocked('export_children_exceed_inspection_bound')
        owners.append(('export_owner:' + export.id, birth, export.phase == 'active'))
        for child in children.values():
            add_process('export:' + export.id + ':' + child['id'], child['identity'])
        documents.append((export.id, export.case_id, export.created_by, export.phase, export.version,
                          export.owner_identity_json, export.launch_nonce, export.profile_json,
                          export.children_json, export.error))
    digest = hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()
    return Snapshot(row.id, digest, tuple(owners), tuple(processes))


async def observe(snapshot):
    """Probe actual recorded births. Never signal, mount, publish or replay."""
    checks = []
    cache = {}

    async def probe(role, distro, birth):
        key = (distro, json.dumps(birth, sort_keys=True))
        if key not in cache:
            cache[key] = (await asyncio.to_thread(identity.probe, birth) if distro is None
                          else await probe_native(distro, birth))
        observed = cache[key]
        checks.append({'role': role, **observed})
        return observed['state']

    acquired = False
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=.1)
        acquired = True
        async with asyncio.timeout(20):
            for role, birth, can_launch in snapshot.owners:
                state = await probe(role, None, birth)
                if state in {'mismatch', 'unverified'}:
                    return result(snapshot.session_id, reason='owner_identity_unverified', checks=checks)
                if state == 'match' and can_launch:
                    return result(snapshot.session_id, reason='owner_still_active', checks=checks)
            for role, distro, birth in snapshot.processes:
                state = await probe(role, distro, birth)
                if state != 'exited':
                    return result(snapshot.session_id, reason='process_still_active' if state == 'match'
                                  else 'process_exit_unverified', checks=checks)
        return result(snapshot.session_id, status='processes_exited',
                      reason='image_and_recovery_operation_not_verified', checks=checks)
    except TimeoutError:
        return result(snapshot.session_id, reason='inspection_timeout' if acquired else 'inspection_busy', checks=checks)
    finally:
        if acquired:
            _slots.release()
