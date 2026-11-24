"""Remote immutable alias plans and conditional compatibility file mutations."""
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
import hashlib
import io
import json

from botocore.exceptions import ClientError

from .. import storage
from . import attempt_objects as objects, attempt_workspace as workspace
from . import publication_objects as bundles, s3_case_files as live
from .case_write import CaseWriteConflict
from .durable_events import json_text
from .s3_streams import snapshot as object_snapshot


def record(data):
    return objects.digest(data) if data is not None else None


def _source_record(prefix, row, manifest, path, *, output=None):
    """Verify one immutable alias source, optionally into admitted scratch."""
    expected = manifest['files'].get(path)
    if expected is None:
        raise FileNotFoundError(path)
    key = bundles.identity(row) + '/files/' + path
    if expected['size'] == 0:
        # Older SeaweedFS releases reject conditional GETs for empty objects.
        return record(objects.checked(objects.read(prefix, key, limit=0), expected))
    observed, _, _ = object_snapshot(prefix, key, output=output, limit=expected['size'])
    if observed is None:
        raise FileNotFoundError(path)
    if observed != {field: expected[field] for field in ('size', 'sha256')}:
        raise CaseWriteConflict('input_stale', 'Immutable S3 object content differs from its manifest')
    return observed


@contextmanager
def _source_body(prefix, row, manifest, operation):
    from .s3_file_transactions import _temporary_copy

    source = operation['source']
    expected = operation['after']
    small = expected['size'] <= 64 * 1024
    with (io.BytesIO() if small else _temporary_copy(expected['size'])) as stream:
        if _source_record(prefix, row, manifest, source, output=stream) != expected:
            raise ValueError('Alias source no longer matches')
        yield stream.getvalue() if small else stream


def prepare(prefix,row,manifest):
    from .publication_alias_journal import operations, SCHEMA
    intent=bundles.read_intent(prefix,row)
    if intent['manifest']!=manifest or intent['manifest_sha256']!=row.manifest_sha256:
        raise ValueError('Alias plan differs from durable bundle intent')
    verified=bundles.bound_metadata(prefix,row)
    if verified!=manifest:raise ValueError('Alias sources differ from the verified bundle')
    from .s3_live_store import Client
    client=storage.s3();snapshot=client.snapshot(objects.get_settings().s3_bucket,prefix) if isinstance(client,Client) else None
    plan=[];checked={}
    for operation in operations(manifest):
        path=workspace.canonical_path(operation['path']);workspace.assert_mutable_path(prefix,path)
        before=client.digest_snapshot(snapshot,path) if snapshot is not None else record(live.read_version(prefix,path)[0])
        source=operation['source'];after=None
        if source:
            if source not in checked:checked[source]=_source_record(prefix,row,manifest,source)
            after=checked[source]
        if 'allowed_before' in operation and before!=after and before not in operation['allowed_before']:
            before=operation['allowed_before'][-1]
        plan.append({'path':path,'source':source,'before':before,'after':after,
            **({'preserve_conflict':True} if operation.get('preserve_conflict') is True else {})})
    # Some handlers expose only a subset as compatibility aliases. Verify every
    # remaining immutable file too, without hydrating/rechecking whole bundles
    # repeatedly. Actual conditional writes still reread their source bytes.
    for source in manifest['files'].keys()-checked.keys():
        _source_record(prefix,row,manifest,source)
    if bundles.bound_metadata(prefix,row)!=manifest:
        raise ValueError('Publication metadata changed during alias preparation')
    if snapshot is not None and not client.matches_snapshot(snapshot,[operation['path'] for operation in plan]):
        raise ValueError('Alias destinations changed during plan preparation')
    value={'schema':SCHEMA,'publication_id':row.id,'case_id':row.case_id,'attempt_id':row.attempt_id,
        'manifest_sha256':row.manifest_sha256,'storage':objects.binding(prefix),'operations':plan}
    encoded=bundles.encoded(value)
    objects.put_new(prefix,bundles.journal_path(row,'aliases.json'),encoded)
    return hashlib.sha256(encoded).hexdigest()


def apply(prefix,row,digest):
    from .publication_alias_journal import SCHEMA, preserves_retired_edit
    encoded=objects.read(prefix,bundles.journal_path(row,'aliases.json'))
    value=json.loads(encoded)
    if (hashlib.sha256(encoded).hexdigest()!=digest or value.get('schema')!=SCHEMA
            or value.get('publication_id')!=row.id or value.get('case_id')!=row.case_id
            or value.get('attempt_id')!=row.attempt_id or value.get('manifest_sha256')!=row.manifest_sha256
            or value.get('storage')!=objects.binding(prefix)):
        raise ValueError('Alias plan no longer matches its committed publication')
    live.require_conditions(prefix)
    manifest=bundles.verified_manifest(prefix,row)
    from .s3_live_store import Client
    client=storage.s3()
    if isinstance(client,Client):
        return apply_snapshot(prefix,row,digest,value,manifest,client)
    preserved=set()
    for operation in value['operations']:
        path=workspace.canonical_path(operation['path']);workspace.assert_mutable_path(prefix,path)
        present,_=live.read_version(prefix,path)
        if record(present) not in (operation['before'],operation['after']):
            if preserves_retired_edit(operation):
                preserved.add(path)
                continue
            raise ValueError('Edited compatibility destination was preserved: '+path)
    for operation in value['operations']:
        if operation['path'] in preserved:continue
        path=operation['path'];present,etag=live.read_version(prefix,path)
        if record(present)==operation['after']:continue
        if record(present)!=operation['before']:
            if preserves_retired_edit(operation):
                preserved.add(path)
                continue
            raise ValueError('Compatibility destination changed: '+path)
        args=objects.arguments(prefix,path)
        try:
            if operation['source'] is None:
                if not etag:raise ValueError('Alias deletion has no checked object version')
                storage.s3().delete_object(**args,IfMatch=etag)
            else:
                condition={'IfMatch':etag} if etag else {'IfNoneMatch':'*'}
                with _source_body(prefix,row,manifest,operation) as body:
                    storage.s3().put_object(**args,Body=body,**condition)
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode']==412:
                if preserves_retired_edit(operation):
                    preserved.add(path)
                    continue
                raise ValueError('Compatibility destination changed during conditional write: '+path) from error
            raise
        observed,_=live.read_version(prefix,path)
        if record(observed)!=operation['after']:
            if preserves_retired_edit(operation):
                preserved.add(path)
                continue
            raise ValueError('Alias changed after conditional write: '+path)
    if preserved:
        raise ValueError('Edited retired solve outputs were preserved: '+', '.join(sorted(preserved)))
    receipt={'schema':SCHEMA,'plan_sha256':digest,'state':'applied'}
    objects.put_new(prefix,bundles.journal_path(row,'aliases-applied.json'),json_text(receipt).encode('utf8'))


class _SupersededAlias(Exception):
    pass


@asynccontextmanager
async def stage_application(prefix, row, digest):
    """Apply the durable alias plan under a case storage lease and outside SQL."""
    from ..models import Case
    from . import artifact_publication, case_copy_io, storage_operations
    from .case_provision import io
    from .case_read_capacity import file_read_deadline
    from .case_write import assert_case_available
    from .request_deadline import STAGING_CLEANUP_SECONDS
    from .s3_storage_recovery import recover_reserved

    async def checked(db):
        case=await db.get(Case,row.case_id,populate_existing=True)
        if case is None or case.s3_prefix!=prefix:
            raise _SupersededAlias
        assert_case_available(case)
        return case

    async def require_head(db,case):
        _,active=await artifact_publication.current(db,case.id,row.kind)
        if active is None or active.id!=row.id:
            raise _SupersededAlias
        if (active.attempt_id,active.manifest_sha256,active.immutable_path)!=(row.attempt_id,row.manifest_sha256,row.immutable_path):
            raise ValueError('Alias publication changed during storage admission')

    async with file_read_deadline('publication_alias','应用发布兼容文件',seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code='publication_alias_busy',label='准备发布兼容文件'):
            async with AsyncExitStack() as scopes:
                try:
                    lease=await scopes.enter_async_context(storage_operations.hold(checked,purpose='staging',
                        validate_locked=require_head))
                    await recover_reserved(lease)
                    async with lease.control() as db:
                        await require_head(db,lease.case)
                    await io(apply,prefix,row,digest)
                except _SupersededAlias:
                    yield False
                    return
                yield True


def apply_snapshot(prefix,row,digest,value,manifest,client,_retry=0):
    from .publication_alias_journal import SCHEMA,preserves_retired_edit
    snapshot=client.snapshot(objects.get_settings().s3_bucket,prefix)
    preserved=set();changes={};watched=[];paths=set()
    for operation in value['operations']:
        path=workspace.canonical_path(operation['path']);workspace.assert_mutable_path(prefix,path)
        if path in paths:raise ValueError('Alias plan has duplicate destinations')
        paths.add(path);present=client.digest_snapshot(snapshot,path)
        if present not in (operation['before'],operation['after']):
            if preserves_retired_edit(operation):preserved.add(path);continue
            raise ValueError('Edited compatibility destination was preserved: '+path)
        watched.append(path)
        if present==operation['after']:continue
        if operation['source'] is None:changes[path]=None
        else:
            def reader(operation=operation):
                return _source_body(prefix,row,manifest,operation)
            changes[path]=reader
    try:committed=client.commit_snapshot(snapshot,changes,watch_paths=watched)
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode']==412:
            latest=client.snapshot(snapshot.bucket,prefix)
            changed={path for path in watched if latest.value['files'].get(path)!=snapshot.value['files'].get(path)}
            independent={operation['path'] for operation in value['operations'] if preserves_retired_edit(operation)}
            if _retry<2 and changed and changed<=independent:
                # Re-read only in this live caller. Edited retired outputs are
                # preserved without blocking current outputs; current-file or
                # ABA conflicts still reject the whole conditional transition.
                return apply_snapshot(prefix,row,digest,value,manifest,client,_retry+1)
            raise ValueError('Compatibility destinations changed during batch publication') from error
        raise
    for operation in value['operations']:
        if operation['path'] in preserved:continue
        if client.digest_snapshot(committed,operation['path'])!=operation['after']:
            raise ValueError('Alias changed after batch publication: '+operation['path'])
    if not client.matches_snapshot(committed,watched):raise ValueError('Alias destinations changed after batch publication')
    if preserved:raise ValueError('Edited retired solve outputs were preserved: '+', '.join(sorted(preserved)))
    receipt={'schema':SCHEMA,'plan_sha256':digest,'state':'applied'}
    objects.put_new(prefix,bundles.journal_path(row,'aliases-applied.json'),json_text(receipt).encode('utf8'))
