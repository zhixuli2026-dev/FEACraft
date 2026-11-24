"""Version-checked live S3 files and measured conditional mutation support."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
import os
import uuid

from botocore.exceptions import ClientError

from .. import storage
from . import attempt_objects as objects, attempt_workspace as workspace
from .case_file_delta import restore_source, parents
from .case_write import CaseWriteConflict


def read_version(prefix,path):
    try:
        info=storage.s3().head_object(**objects.arguments(prefix,path))
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode']==404:return None,None
        raise
    if not info.get('ETag') or not 0<=info['ContentLength']<=workspace.MAX_BYTES:
        raise ValueError('Live S3 object has no admitted version or size')
    data=objects.read(prefix,path,etag=info['ETag'],limit=info['ContentLength'])
    if len(data)!=info['ContentLength']:raise ValueError('Live S3 object size changed')
    return data,info['ETag']


@dataclass(frozen=True)
class SourceObservation:
    binding: dict
    versions: dict
    source: dict

    def verify(self, prefix):
        if objects.binding(prefix) != self.binding or objects.selection(prefix, [], True) != self.versions:
            raise CaseWriteConflict('input_stale', 'Live S3 sources changed during inspection')


def observe_source(prefix, *, catalog_only=False, windows_modes=True):
    """Digest live S3 inputs in place, preserving the portable source records."""
    from ..storage_paths import contains_file_stage
    from .s3_live_store import Client, split, visible

    if not storage._use_s3():
        return None
    binding = objects.binding(prefix)
    client = storage.s3()
    catalog = None
    if isinstance(client, Client) and split(prefix + '/') == (prefix, ''):
        catalog = client.public_snapshot(objects.get_settings().s3_bucket, prefix)
    if catalog_only and catalog is None:
        return None

    versions = objects.selection(prefix, [], True)
    if catalog is not None:
        entries = {path: {'size':entry['size'], 'etag':entry['etag'].strip('"')}
                   for path, entry in catalog.value['files'].items()
                   if visible(entry) and path.split('/')[0].rstrip(' .').casefold() != '.vibeflow'}
        if entries != versions:
            raise CaseWriteConflict('input_stale', 'Live S3 sources changed before inspection')

    def digest(path):
        try:
            if catalog is None:
                record = objects.selected_digest(prefix, path, **versions[path])
            else:
                record = client.digest_snapshot(catalog, path)
        except FileNotFoundError as error:
            raise CaseWriteConflict('input_stale', 'Live S3 source disappeared from its snapshot', path=path) from error
        if record is None:
            raise CaseWriteConflict('input_stale', 'Live S3 source disappeared from its snapshot', path=path)
        # The old typed-command copy used chmod(0o644). On Windows stat
        # still marks these executable suffixes, including bare ".exe".
        executable = windows_modes and os.name == 'nt' and path.casefold().endswith(('.bat', '.cmd', '.com', '.exe'))
        return path, {**record, 'executable':executable}

    paths = [path for path in versions if not contains_file_stage(path)]
    if catalog is None:
        files = dict(map(digest, paths))
    else:
        context = copy_context()
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='case-source-digest') as pool:
            files = dict(pool.map(lambda path: context.copy().run(digest, path), paths))
    observation = SourceObservation(binding, versions,
                                    {'files':files, 'directories':sorted(parents(files))})
    observation.verify(prefix)
    return observation


def snapshot(prefix, *, catalog_only=False, windows_modes=True):
    observed = observe_source(prefix, catalog_only=catalog_only, windows_modes=windows_modes)
    return observed.source if observed is not None else None


def current_source(prefix,descriptor):
    observed=restore_source(snapshot(prefix),descriptor)
    # S3 has no inferred empty directories. Reconstitute parents of restored
    # input files when a published deletion removed the last object in a dir.
    observed['directories']=sorted(parents(observed['files']))
    return observed


class UnsupportedConditions(ValueError):
    """A probe demonstrated that an S3 backend ignores a required condition."""


def require_conditions(prefix):
    """Probe only fresh owned keys; never trust an ignored HTTP precondition."""
    from .storage_operations import current

    client=storage.s3()
    lease=current()
    bound=objects.binding(prefix)
    verification=(client,bound['endpoint'],bound['bucket'],prefix)
    if lease is not None and lease.verified_s3_conditions==verification:
        return
    path='.vibeflow/publications/.capabilities/'+uuid.uuid4().hex+'/probe'
    args=objects.arguments(prefix,path)
    def denied(call):
        try:call()
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode']==412:return
            raise
        raise UnsupportedConditions('S3 backend does not enforce conditional create/update/delete; use a compatible server')
    try:
        first=client.put_object(**args,Body=b'original',IfNoneMatch='*')['ETag']
        denied(lambda:client.put_object(**args,Body=b'wrong-create',IfNoneMatch='*'))
        denied(lambda:client.put_object(**args,Body=b'wrong-update',IfMatch='"invalid-version"'))
        denied(lambda:client.delete_object(**args,IfMatch='"invalid-version"'))
        if objects.read(prefix,path,limit=8)!=b'original':raise ValueError('Conditional probe changed unexpectedly')
        second=client.put_object(**args,Body=b'replaced',IfMatch=first)['ETag']
        if objects.read(prefix,path,limit=8)!=b'replaced':raise UnsupportedConditions('Conditional update is unavailable')
        client.delete_object(**args,IfMatch=second)
        try:objects.read(prefix,path)
        except FileNotFoundError:pass
        else:raise UnsupportedConditions('Conditional deletion is unavailable')
    finally:
        # This unpredictable key is exclusively owned by this probe. Never
        # perform this unconditional cleanup on an alias or a publication file.
        client.delete_object(**args)
    if lease is not None and lease.case.s3_prefix==prefix:
        lease.verified_s3_conditions=verification
