"""Short-lived, version-checked case input views for filesystem-based readers.

These directories never serve as live output destinations. Registered workers
still run only inside their independently sealed attempt workspace.
"""
from contextlib import contextmanager, asynccontextmanager
import asyncio

from botocore.exceptions import ClientError

from .. import storage
from . import attempt_objects as objects
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .private_cache import Cache


@contextmanager
def view(prefix, paths=None, *, optional_paths=(), max_file_bytes=None):
    if not storage._use_s3():
        from .local_source_reads import observe
        root=storage._fs_path(prefix,'.')
        selected=None if paths is None else (*paths,*optional_paths)
        with observe(root,selected):
            yield root
        return
    selected=[] if paths is None else paths
    def versions_now():
        return objects.selection(prefix,selected,paths is None,optional_paths=optional_paths)
    versions=versions_now()
    if max_file_bytes is not None and any(item['size'] > max_file_bytes for item in versions.values()):
        raise ValueError(f'所选文件超过 {max_file_bytes // (1024 * 1024)} MiB 限额')
    cache=Cache('.case-read-views')
    root=cache.root
    try:
        items = ((path,version['size'],
            lambda path=path,version=version:objects.selected_stream(
                prefix,path,etag=version['etag'],size=version['size']))
            for path,version in versions.items())
        cache.fetch_many(items, stream=True)
        if versions_now()!=versions:
            raise CaseWriteConflict('input_stale','S3 case changed during input materialization')
        yield root
        if versions_now()!=versions:
            raise CaseWriteConflict('input_stale','S3 case changed during input inspection')
    finally:
        cache.cleanup()


def inspect(prefix, reader, *args, paths=None, optional_paths=(), max_file_bytes=None, **kwargs):
    with view(prefix,paths,optional_paths=optional_paths,max_file_bytes=max_file_bytes) as root:
        return reader(root,*args,**kwargs)


def remote_live_version(prefix):
    """Version of the public tree and its logical portable modes."""
    from .s3_live_store import HEAD, missing
    from .tree_metadata_native import PATH as mode_path

    try:
        catalog = storage.s3().head_object(**objects.arguments(prefix, HEAD))
    except ClientError as error:
        if not missing(error):
            raise
        return {row['path']: (row['size'], row['etag']) for row in storage._s3_list(prefix)
                if row['path'] == mode_path or row['path'].split('/')[0].rstrip(' .').casefold() != '.vibeflow'}
    if not catalog.get('ETag'):
        raise CaseWriteConflict('input_stale', '当前算例清单缺少可核验的对象版本')
    return catalog['ETag']


def _case_catalog_versions(prefix):
    from ..config import get_settings
    from .s3_live_store import Client

    client = storage.s3()
    if isinstance(client, Client):
        rows = client.public_file_rows(get_settings().s3_bucket, prefix)
        if rows is not None:
            return {row['path']: {'size': row['size'], 'etag': row['etag']} for row in rows}
    return None


def _remote_file_version(prefix, path):
    from .file_read import FileRead

    try:
        reader = FileRead(prefix, path)
    except FileNotFoundError:
        return None
    return {'size': reader.size, 'etag': reader.etag.strip('"')}


def remote_file_versions(prefix, paths):
    catalog = _case_catalog_versions(prefix)
    if catalog is not None:
        return {path: catalog.get(path) for path in paths}
    return {path: _remote_file_version(prefix, path) for path in paths}


class SelectedCaseFiles:
    """Resolve a dynamic set of exact, versioned case files into a private cache."""

    def __init__(self, prefix, cache):
        self.prefix = prefix
        self.cache = cache
        self.root = cache.root
        self.catalog = _case_catalog_versions(prefix)
        self.versions = {}
        self.files = 0
        self.bytes = 0

    def load(self, path):
        from ..storage_paths import contains_file_stage
        from .attempt_workspace import canonical_path, MAX_BYTES, MAX_FILES
        from .request_deadline import current

        if canonical_path(path) != path or path.split('/')[0].casefold() == '.vibeflow' or contains_file_stage(path):
            raise ValueError('Report input is outside the public case files')
        if path in self.versions:
            return self.versions[path] is not None
        deadline = current()
        if deadline is not None:
            deadline.check()
        version = self.catalog.get(path) if self.catalog is not None else _remote_file_version(self.prefix, path)
        if version is None:
            self.versions[path] = None
            return False
        size = version['size']
        if not version['etag'] or type(size) is not int or size < 0:
            raise ValueError('Report input has no valid conditional read version')
        if self.files >= MAX_FILES or self.bytes + size > MAX_BYTES:
            raise ValueError('Report input selection exceeds its file count or storage limit')
        try:
            self.cache.fetch_many([(path, size, lambda: objects.selected_stream(self.prefix, path, **version))],
                                  stream=True)
        except FileNotFoundError as error:
            raise CaseWriteConflict('input_stale', 'S3 report input disappeared during reading', path=path) from error
        self.versions[path] = version
        self.files += 1
        self.bytes += size
        return True


@contextmanager
def selected_view(prefix):
    """Validate selected versions and missing candidates before releasing the view."""
    cache = Cache('.case-read-views')
    try:
        selected = SelectedCaseFiles(prefix, cache)
        yield selected
        if remote_file_versions(prefix, selected.versions) != selected.versions:
            raise CaseWriteConflict('input_stale', 'S3 report inputs changed during inspection')
    finally:
        cache.cleanup()


def delta_source(prefix, descriptor):
    """Restore inputs behind a published whole-case delta on either backend."""
    from .case_file_delta import current_source, parents, restore_event_prefix
    observed=restore_event_prefix(prefix,descriptor,inspect(prefix,current_source,descriptor))
    if storage._use_s3():
        # Object stores do not retain empty directories after a last-file delete.
        observed['directories']=sorted(parents(observed['files']))
    return observed


@asynccontextmanager
async def async_view(prefix,paths=None,*,optional_paths=()):
    async with async_read_view(view(prefix,paths,optional_paths=optional_paths)) as root:
        yield root


@asynccontextmanager
async def async_read_view(context):
    entering=asyncio.create_task(asyncio.to_thread(context.__enter__))
    try:
        root=await finish_before_cancel(entering)
    except BaseException as error:
        if entering.done() and not entering.cancelled() and entering.exception() is None:
            await finish_before_cancel(asyncio.to_thread(context.__exit__,type(error),error,error.__traceback__))
        raise
    try:
        yield root
    except BaseException as error:
        await finish_before_cancel(asyncio.to_thread(context.__exit__,type(error),error,error.__traceback__))
        raise
    else:
        await finish_before_cancel(asyncio.to_thread(context.__exit__,None,None,None))
