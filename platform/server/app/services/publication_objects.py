"""Immutable S3 publication bundles; local directories are verified read caches.

Conditional object creation makes copying retryable. Only the existing SQL
ownership and publication-head transaction can make a bundle visible.
"""
import hashlib
import json
import os
import re
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar

from .. import storage
from . import attempt_objects as objects, attempt_workspace as workspace
from .durable_events import json_text
from .fs_directory import DirectoryMoveError, move_new, remove_owned

MAX_MANIFEST = 16 * 1024 * 1024
_active_reads = ContextVar('publication_object_reads', default=None)


class PublicationObjectReads:
    """Pin the authenticated object inventories read during detached admission."""

    def __init__(self):
        self.versions = {}

    @contextmanager
    def collect(self):
        token = _active_reads.set(self)
        try:
            yield
        finally:
            _active_reads.reset(token)

    def require_current(self):
        from .publication_inputs import PublicationInputStale

        for (prefix, target), expected in self.versions.items():
            if storage._s3_list(prefix, target) != expected:
                raise PublicationInputStale('校验后的发布存档对象已变化')


@contextmanager
def _observe_archive(prefix, row):
    reads = _active_reads.get()
    if reads is None:
        yield
        return
    from .publication_inputs import PublicationInputStale

    target = identity(row)
    before = storage._s3_list(prefix, target)
    if not before or any(not item['etag'] for item in before):
        raise PublicationInputStale('发布存档缺少可核验的对象版本')
    yield
    if (storage._s3_list(prefix, target) != before
            or reads.versions.setdefault((prefix, target), before) != before):
        raise PublicationInputStale('读取期间发布存档对象发生变化')


def identity(row):
    if (str(uuid.UUID(row.id)) != row.id or str(uuid.UUID(row.attempt_id)) != row.attempt_id
            or row.immutable_path != '.vibeflow/publications/' + row.id):
        raise ValueError('Invalid publication object identity')
    return row.immutable_path


def validate_records(records):
    if not isinstance(records, dict) or len(records) > workspace.MAX_FILES:
        raise ValueError('Invalid publication object inventory')
    names = {}; total = 0
    for path, record in records.items():
        if workspace.canonical_path(path) != path:
            raise ValueError('Publication object path is not canonical')
        if (not isinstance(record, dict) or type(record.get('size')) is not int or record['size'] < 0
                or not isinstance(record.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])
                or type(record.get('executable')) is not bool):
            raise ValueError('Invalid publication object record')
        total += record['size']
        parts = path.split('/')
        for length in range(1, len(parts) + 1):
            name = '/'.join(parts[:length]); kind = 'file' if length == len(parts) else 'directory'
            previous = names.get(name.casefold())
            if previous is not None and previous != (name, kind):
                raise ValueError('Publication object path collision')
            names[name.casefold()] = (name, kind)
    if total > workspace.MAX_BYTES:
        raise ValueError('Publication object size limit exceeded')


def validate_manifest(prefix, row, manifest):
    from .artifact_publication import SCHEMA
    identity(row)
    if (manifest.get('schema') != SCHEMA or manifest.get('storage') != objects.binding(prefix)
            or any(manifest.get(key) != getattr(row, field) for key, field in
                   [('publication_id','id'), ('case_id','case_id'), ('attempt_id','attempt_id'),
                    ('kind','kind'), ('generation','generation')])):
        raise ValueError('Publication object ownership or storage differs from reservation')
    validate_records(manifest['files'])


def encoded(value):
    data = json_text(value).encode('utf8')
    if len(data) > MAX_MANIFEST:
        raise ValueError('Publication object manifest exceeds 16 MiB')
    return data


def journal_path(row, name):
    from .publication_journal import DIRECTORY
    identity(row)
    if name not in {'intent.json', 'ready.json', 'aliases.json', 'aliases-applied.json'}:
        raise ValueError('Unsupported publication object receipt')
    return DIRECTORY + '/' + row.id + '/' + name


def read_intent(prefix, row):
    from .publication_journal import SCHEMA
    value = json.loads(objects.read(prefix, journal_path(row, 'intent.json')))
    manifest = value['manifest']
    validate_manifest(prefix, row, manifest)
    if value.get('schema') != SCHEMA or value.get('manifest_sha256') != hashlib.sha256(encoded(manifest)).hexdigest():
        raise ValueError('Publication object intent digest differs')
    return value


def record_intent(prefix, row, manifest):
    from .publication_journal import SCHEMA
    validate_manifest(prefix, row, manifest)
    digest = hashlib.sha256(encoded(manifest)).hexdigest()
    value = {'schema':SCHEMA, 'manifest_sha256':digest, 'manifest':manifest}
    objects.put_new(prefix, journal_path(row, 'intent.json'), encoded(value))
    return digest


def _bound_snapshot(prefix, row):
    target=identity(row);data=objects.read(prefix,target+'/manifest.json')
    if hashlib.sha256(data).hexdigest()!=row.manifest_sha256:
        raise ValueError('Publication object manifest differs from database')
    manifest=json.loads(data);validate_manifest(prefix,row,manifest)
    expected={'manifest.json'}|{'files/'+path for path in manifest['files']}
    observed = storage._s3_list(prefix, target)
    if {item['path'] for item in observed} != expected:
        raise ValueError('Publication object inventory differs from manifest')
    return manifest, observed


def bound_metadata(prefix,row):
    """Authenticate metadata and inventory; caller must still check file bytes."""
    return _bound_snapshot(prefix, row)[0]


def verified_manifest(prefix, row):
    """Check the immutable bundle in S3 without building a local read cache."""
    with _observe_archive(prefix, row):
        manifest, versions = _bound_snapshot(prefix, row)
        objects.verify_members(prefix, identity(row) + '/files', manifest['files'])
        if _bound_snapshot(prefix, row) != (manifest, versions):
            raise ValueError('Publication objects changed during remote verification')
        return manifest


def verify(prefix, row, manifest):
    """Never accept a matching local cache as proof of remote integrity."""
    validate_manifest(prefix, row, manifest)
    target = identity(row)
    data = objects.read(prefix, target + '/manifest.json')
    if data != encoded(manifest):
        raise ValueError('Publication object manifest differs from validated intent')
    expected = {'manifest.json'} | {'files/' + path for path in manifest['files']}
    if {item['path'] for item in storage._s3_list(prefix, target)} != expected:
        raise ValueError('Publication object inventory differs from manifest')
    objects.verify_members(prefix,target+'/files',manifest['files'])
    return data


def prepare(prefix, row, run, manifest):
    from .publication_journal import SCHEMA
    digest = record_intent(prefix, row, manifest)
    target = identity(row)
    try:
        objects.read(prefix, target + '/manifest.json')
    except FileNotFoundError:
        if run is None:
            raise ValueError('Verified publication disappeared before readiness recovery')
        for path, record in manifest['files'].items():
            source = workspace.checked_path(run, path)
            objects.put_output_member(prefix, target + '/files', path, source, record)
        # A crash before this final object leaves only a recoverable copy intent.
        objects.put_new(prefix, target + '/manifest.json', encoded(manifest))
    verify(prefix, row, manifest)
    objects.put_new(prefix, journal_path(row, 'ready.json'), encoded(
        {'schema':SCHEMA, 'manifest_sha256':digest, 'state':'bundle_ready'}))
    return digest


def read_manifest(prefix, row):
    with _observe_archive(prefix, row):
        return _read_manifest(prefix, row)


def _read_manifest(prefix, row):
    target_path = identity(row)
    data = objects.read(prefix, target_path + '/manifest.json')
    if hashlib.sha256(data).hexdigest() != row.manifest_sha256:
        raise ValueError('Publication object manifest differs from database')
    manifest = json.loads(data)
    validate_manifest(prefix, row, manifest)
    root = storage._fs_path(prefix, '.')
    target = root / target_path
    stage = None
    reservations=ExitStack()
    admitted=False
    remote_verified=False
    from .publication_journal import _verify_bundle
    try:
        if not os.path.lexists(target):
            from .node_storage_budget import reserve,copy_bytes
            reservations.enter_context(reserve(copy_bytes(manifest['files'],data)))
            admitted=True
        if os.path.lexists(target):
            verify(prefix, row, manifest)
            remote_verified=True
        elif bound_metadata(prefix,row)!=manifest:
            raise ValueError('Publication metadata changed before cache restoration')
        root.mkdir(parents=True, exist_ok=True)
        parent = root
        for part in '.vibeflow/publications'.split('/'):
            parent /= part; parent.mkdir(exist_ok=True); workspace.ordinary(parent)
        if not os.path.lexists(target):
            if not admitted:
                # A cache removed after the initial observation needs admission too.
                from .node_storage_budget import reserve,copy_bytes
                reservations.enter_context(reserve(copy_bytes(manifest['files'],data)))
            stage = parent / ('.restoring-' + row.id + '-' + uuid.uuid4().hex)
            stage.mkdir(); (stage / 'files').mkdir()
            # The same actual streams authenticate each remote member and
            # populate the owned cache. Avoid a separate full remote pass and
            # serial re-download when the cache is initially absent.
            objects.verify_members(prefix,target_path+'/files',manifest['files'],destination=stage/'files')
            if bound_metadata(prefix,row)!=manifest:
                raise ValueError('Publication metadata changed during cache restoration')
            remote_verified=True
            with (stage / 'manifest.json').open('xb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            _verify_bundle(root, stage, manifest)
            try:
                move_new(stage, target, root=root)
            except (FileExistsError, DirectoryMoveError):
                if not target.exists(): raise
        if not remote_verified:
            # A peer may have installed the cache after our metadata check.
            # Its local files cannot replace this reader's remote verification.
            verify(prefix,row,manifest)
        _verify_bundle(root, target, manifest)
        return target, manifest
    finally:
        try:
            if stage is not None and stage.exists(): remove_owned(stage, root=root)
        finally:reservations.close()


def inspect(prefix, row):
    identity(row)
    try:
        try:
            intent = read_intent(prefix, row)
        except FileNotFoundError:
            return {'state':'legacy_unjournaled'}
        digest = intent['manifest_sha256']
        if row.manifest_sha256 not in ({digest} if row.status == 'committed' else {'0'*64, digest}):
            raise ValueError('Database digest differs from publication object intent')
        try:
            objects.read(prefix, row.immutable_path + '/manifest.json')
        except FileNotFoundError:
            if row.status == 'committed': raise ValueError('Committed publication manifest is missing')
            return {'state':'copy_incomplete', 'manifest_sha256':digest}
        verify(prefix, row, intent['manifest'])
        return {'state':'bundle_verified', 'manifest_sha256':digest}
    except (OSError, ValueError, KeyError, TypeError):
        return {'state':'requires_attention'}
