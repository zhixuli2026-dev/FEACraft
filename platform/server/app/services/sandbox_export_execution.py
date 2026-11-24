"""Owned, bounded sandbox file retention independent of terminal termination.

The caller must complete export before publishing case changes or recording a
successful terminal close. This module retains private evidence on failure.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import secrets
import uuid

from .. import storage
from ..db import SessionLocal
from ..models import NativeSession
from . import native_execution_client as client, sandbox_export_native as native
from . import sandbox_volume, terminal_records, process_stop
from .async_ops import finish_before_cancel
from .attempt_workspace import checked_path, ordinary
from .owned_process import OwnedProcess, probe_native, wsl_path

NATIVE = Path(native.__file__)
DIRECTORY = '.terminal-export'
LIFETIME = 14400
CODE = ('sandbox_export_native.py', 'sandbox_volume.py', 'process_identity_native.py', 'process_stop.py')


def code():
    return {name: hashlib.sha256(NATIVE.with_name(name).read_bytes()).hexdigest() for name in CODE}


def profile(value):
    fields={'session_id', 'root', 'gate', 'terminal_process', 'distro', 'nonce', 'lifetime', 'code'}
    if (not isinstance(value, dict) or set(value) not in (fields,fields|{'filesystem'})
            or value.get('filesystem','tmpfs') not in {'tmpfs','ext4'}
            or not isinstance(value['distro'], str) or not re.fullmatch('[a-z0-9-]+', value['distro'])
            or str(uuid.UUID(value['session_id'])) != value['session_id']
            or not isinstance(value['nonce'], str) or not re.fullmatch('[0-9a-f]{64}', value['nonce'])
            or type(value['lifetime']) is not int or not 10 <= value['lifetime'] <= LIFETIME
            or not isinstance(value['code'], dict) or set(value['code']) != set(CODE)
            or any(not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) for digest in value['code'].values())):
        raise ValueError('Invalid sandbox export profile')
    sandbox_volume.identity_module().validate(value['gate'])
    from .process_record import validate
    validate(value['terminal_process'], bound=False)
    if value['gate']['platform'] != 'linux':
        raise ValueError('Sandbox export requires a Linux gate')
    root = Path(value['root'])
    parent = storage._fs_root().resolve() / DIRECTORY
    if root.parent != parent or not re.fullmatch('[0-9a-f]{32}', root.name):
        raise ValueError('Sandbox export is outside its private storage directory')
    ordinary(parent)
    ordinary(root)
    return value


async def check_session(db, case_id, user_id, selected, owner_birth):
    row = await db.get(NativeSession, selected['session_id'], populate_existing=True)
    if (row is None or row.case_id != case_id or row.created_by != user_id or row.shell != 'wsl-sandbox'
            or row.phase != 'active' or json.loads(row.owner_identity_json) != owner_birth):
        raise ValueError('Sandbox export has no matching live terminal owner')
    journal, _ = terminal_records.document(row)
    terminal=json.loads(row.profile_json)['terminal']
    persistent='--persistent-export' in terminal['command']
    if (selected.get('filesystem','tmpfs')!=('ext4' if persistent else 'tmpfs')
            or (persistent and wsl_path(selected['root'])!=terminal['command'][-1])):
        raise ValueError('Sandbox exporter differs from its registered persistent volume')
    if (journal['gate']['process'] != selected['gate'] or journal['root'] != selected['terminal_process']
            or json.loads(row.profile_json)['distro'] != selected['distro']):
        raise ValueError('Sandbox export gate differs from its terminal')


def settings(selected):
    return {'schema': native.SCHEMA, 'root': wsl_path(selected['root']),
            'filesystem':selected.get('filesystem','tmpfs'),
            **{name: selected[name] for name in ('gate', 'terminal_process', 'nonce', 'lifetime', 'code')}}


def read(root, name, limit):
    path = checked_path(root, name)
    if path.stat().st_size > limit:
        raise ValueError('Sandbox receipt exceeds its bound')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Sandbox receipt exceeds its bound')
    return raw


def command(registered, parameters):
    profile(registered)
    if parameters != {'distro': registered['distro'], 'session_id': registered['session_id']}:
        raise ValueError('Sandbox export call differs from its registered profile')
    if code() != registered['code']:
        raise ValueError('Sandbox exporter changed after registration')
    config = Path(registered['root']) / 'settings.json'
    raw = read(config.parent, config.name, native.MAX_CONFIG)
    if json.loads(raw) != settings(registered):
        raise ValueError('Sandbox export settings differ from registration')
    return ['wsl.exe', '-d', registered['distro'], '--exec', 'python3', '-I', '-S', '-u',
            wsl_path(NATIVE), '--config', wsl_path(config), '--sha256', hashlib.sha256(raw).hexdigest()], registered['lifetime'] + 180, 65536


def run(registered):
    result = client.run_native('sandbox_export', {'distro': registered['distro'], 'session_id': registered['session_id']},
                              timeout=registered['lifetime'] + 180, capture=True)
    if result.returncode:
        native.write(Path(registered['root']), 'diagnostic.txt', result.stdout)
        raise client.ChildExecutionError('Registered sandbox exporter failed')


class SandboxExport:
    def __init__(self, owner):
        self.owner = owner
        self.selected = None
        self.root = None
        self.request_id = None
        self.executor = None
        self.task = None
        self.initial = None
        self.manifest = None
        self.signalled = False
        self.image_allocation = None

    async def reserve_image(self):
        from .node_storage_budget import allocation
        if self.root is not None:raise ValueError('Sandbox disk root already reserved')
        self.image_allocation=allocation(sandbox_volume.MAX_BYTES+65536)
        await self.image_allocation.__aenter__()
        def prepare():
            parent=storage._fs_root().resolve()/DIRECTORY
            parent.mkdir(exist_ok=True);ordinary(parent)
            self.root=parent/uuid.uuid4().hex;self.root.mkdir(exist_ok=False)
        await finish_before_cancel(asyncio.to_thread(prepare))
        return self.root

    async def release_image_reservation(self):
        if self.image_allocation is not None:
            operation=self.image_allocation;self.image_allocation=None
            await finish_before_cancel(operation.__aexit__(None,None,None))

    async def discard_unreleased(self, transport, stop):
        if self.root is None:
            return
        if self.task is not None or transport.released or transport.release_attempted:
            raise ValueError('Sandbox image may contain user changes')
        parent=storage._fs_root().resolve()/DIRECTORY
        if self.root.parent!=parent or not re.fullmatch('[0-9a-f]{32}',self.root.name):
            raise ValueError('Unused sandbox directory is outside private storage')
        ordinary(parent)
        try:ordinary(self.root)
        except FileNotFoundError:return
        if transport.owned.proc is None:
            await asyncio.to_thread(self.root.rmdir)
            return
        process_stop.validate(stop,transport.owned.record)
        if stop['verified'] is not True:
            raise ValueError('Sandbox process stop is unverified')
        if transport.gate_birth is not None:
            observed=await probe_native(transport.distro,transport.gate_birth['process'])
            if observed['state']!='exited':
                raise ValueError('Sandbox terminal gate has no exit proof')
        script=NATIVE.with_name('sandbox_unused_image.py').resolve()
        reader=OwnedProcess()
        try:
            await reader.prepare(['wsl.exe','-d',transport.distro,'--user','root','--exec','python3','-I','-S','-u',
                wsl_path(script),wsl_path(self.root)],cwd=script.parent,env=None,runtime='wsl-sandbox')
            await reader.verify_registration();await reader.release()
            if await reader.wait_root_exit(timeout=20)!=0 or self.root.exists():
                raise RuntimeError('Unreleased sandbox directory was retained')
            await reader.finish()
        finally:
            try:
                if reader.proc is not None and not reader.closed:
                    await finish_before_cancel(reader.stop(reason='executor_error',grace_ms=0))
            finally:reader.close()

    async def start(self):
        if self.task is not None:raise ValueError('Sandbox exporter already started')
        self.task = asyncio.create_task(self._lifetime())
        try:
            async with asyncio.timeout(60):
                while True:
                    if self.task.done():
                        await self.task
                        raise RuntimeError('Sandbox exporter exited before pinning')
                    if self.root is not None and (self.root / 'ready.json').exists():
                        await finish_before_cancel(asyncio.to_thread(self._read_ready))
                        from .sandbox_baseline import bind
                        from .durable_events import lock_case_execution
                        async with SessionLocal() as db:
                            existing = await db.get(NativeSession, self.owner.id)
                            if existing is None:raise ValueError('Sandbox session disappeared')
                            await lock_case_execution(db, existing.case_id)
                            row = await terminal_records.current(db, self.owner, launch=True)
                            await bind(db, row, self.selected, self.initial)
                            await finish_before_cancel(db.commit())
                        return self
                    await asyncio.sleep(.05)
        except BaseException:
            await finish_before_cancel(self.abort())
            raise

    def _read_ready(self):
        ready = json.loads(read(self.root, 'ready.json', native.MAX_CONFIG))
        raw = read(self.root, 'initial.json', sandbox_volume.MAX_MANIFEST)
        initial = json.loads(raw)
        if (ready != {'schema': native.SCHEMA, 'nonce': self.selected['nonce'], 'binding': initial['binding'],
                      'initial_sha256': hashlib.sha256(raw).hexdigest()}
                or initial['schema'] != 'vibeflow.sandbox-volume.v1'
                or initial['binding']['gate'] != self.selected['gate']):
            raise ValueError('Sandbox pin receipt differs from its registered gate')
        self.initial = initial

    async def _lifetime(self):
        from .node_storage_budget import allocation
        from .native_request_execution import scope
        # Includes both manifests, output data and bounded control receipts.
        async with allocation(sandbox_volume.MAX_BYTES + 3 * sandbox_volume.MAX_MANIFEST + 65536):
            async with SessionLocal() as db:
                row = await terminal_records.current(db, self.owner, launch=True)
                journal, _ = terminal_records.document(row)
                if row.shell != 'wsl-sandbox' or journal['gate'] is None:
                    raise ValueError('Only a registered sandbox terminal can pin a volume')
                case_id, user_id = row.case_id, row.created_by
                selected = {'session_id': row.id, 'gate': journal['gate']['process'], 'terminal_process': journal['root'],
                    'distro': json.loads(row.profile_json)['distro'], 'nonce': secrets.token_hex(32),
                    'lifetime': LIFETIME, 'code': code(),
                    'filesystem':'ext4' if '--persistent-export' in json.loads(row.profile_json)['terminal']['command'] else 'tmpfs'}
                await db.commit()
            def prepare():
                parent = storage._fs_root().resolve() / DIRECTORY
                parent.mkdir(exist_ok=True)
                ordinary(parent)
                root = self.root
                if root is None:
                    root=parent / uuid.uuid4().hex;root.mkdir(exist_ok=False)
                selected['root'] = str(root)
                profile(selected)
                native.write(root, 'settings.json', json.dumps(settings(selected)).encode())
                return root
            self.root = await finish_before_cancel(asyncio.to_thread(prepare))
            self.selected = selected
            async with scope(case_id, user_id, self.root, 'terminal.export', selected) as executor:
                self.executor = executor
                self.request_id = executor.owner.id
                await executor.run_sync(run, selected, timeout=LIFETIME + 240)

    async def abort(self):
        if self.task is None:
            return
        if not self.task.done():
            if self.executor is not None:
                self.executor.stop('cancelled')
            else:
                self.task.cancel()
        await finish_before_cancel(asyncio.gather(self.task, return_exceptions=True))

    async def export(self, root, stop):
        if self.task is None or self.initial is None or self.signalled:
            raise ValueError('Sandbox export is not awaiting verified terminal cleanup')
        process_stop.validate(stop, root)
        if stop['verified'] is not True:
            raise ValueError('Sandbox export requires full terminal scope exit proof')
        async with SessionLocal() as db:
            row = await terminal_records.current(db, self.owner)
            journal, _ = terminal_records.document(row)
            if journal['root'] != root or journal['gate']['process'] != self.selected['gate']:
                raise ValueError('Sandbox cleanup proof belongs to another terminal')
            await db.commit()
        if self.task.done():
            await self.task
            raise RuntimeError('Sandbox volume holder exited without export')
        await finish_before_cancel(asyncio.to_thread(native.write, self.root, 'command.json', json.dumps({
            'schema': native.SCHEMA, 'nonce': self.selected['nonce'], 'action': 'export', 'stop': stop}).encode()))
        self.signalled = True
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 180)
            self.manifest = await finish_before_cancel(asyncio.to_thread(self._read_complete))
            return self.manifest
        except BaseException:
            await finish_before_cancel(self.abort())
            raise

    def _read_complete(self):
        complete = json.loads(read(self.root, 'complete.json', native.MAX_CONFIG))
        raw = read(self.root, 'manifest.json', sandbox_volume.MAX_MANIFEST)
        manifest = json.loads(raw)
        if (complete != {'schema': native.SCHEMA, 'nonce': self.selected['nonce'],
                         'manifest_sha256': hashlib.sha256(raw).hexdigest()}
                or manifest['schema'] != 'vibeflow.sandbox-volume.v1'
                or manifest['binding'] != self.initial['binding']):
            raise ValueError('Sandbox export receipt differs from its pinned volume')
        return manifest
