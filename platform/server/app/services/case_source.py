"""Observed source manifests for coordinated copies/backups, not native snapshots.

Callers lock the source case before reading and keep that lock through use.
Hash checks detect observed bypass changes; they cannot prevent external writes.
"""
import hashlib
import io
import os
from dataclasses import dataclass

from .. import storage
from . import file_transactions
from .request_deadline import current


class SourceChanged(ValueError):
    pass


def _changed(path):
    return SourceChanged(f"读取期间源文件 {path} 发生变化，请重试")


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class CopySource:
    """Content with an approved inventory, copied into an admitted destination."""

    size: int
    sha256: str

    @property
    def record(self):
        return {'size': self.size, 'sha256': self.sha256}


@dataclass(frozen=True)
class SourceFile(CopySource):
    prefix: str
    path: str
    size: int
    sha256: str
    version: str | tuple[int, ...]

    def copy_into(self, destination):
        """Read exactly the approved version into a caller-admitted destination."""
        try:
            if storage._use_s3():
                from .s3_streams import snapshot_from_head
                observed, _, _ = snapshot_from_head(self.prefix, self.path,
                    {'ContentLength': self.size, 'ETag': self.version}, output=destination)
            else:
                target = storage._fs_path(self.prefix, self.path)
                if _identity(target.stat()) != self.version:
                    raise _changed(self.path)
                observed = file_transactions._capture(target, destination, limit=self.size, deadline=current())
                if _identity(target.stat()) != self.version:
                    raise _changed(self.path)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError) as error:
            raise _changed(self.path) from error
        if observed != self.record:
            raise _changed(self.path)

    def read_content(self):
        """Materialize only case metadata that the constructor must transform."""
        if storage._use_s3():
            with io.BytesIO() as buffer:
                self.copy_into(buffer)
                return buffer.getvalue()
        try:
            target = storage._fs_path(self.prefix, self.path)
            if _identity(target.stat()) != self.version:
                raise _changed(self.path)
            with target.open('rb') as stream:
                before = os.fstat(stream.fileno())
                data = stream.read(self.size+1)
                after = os.fstat(stream.fileno())
            if (_identity(before) != self.version or _identity(after) != self.version
                    or _identity(target.stat()) != self.version or len(data) != self.size
                    or hashlib.sha256(data).hexdigest() != self.sha256):
                raise _changed(self.path)
            return data
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as error:
            raise _changed(self.path) from error


def source_manifest(prefix, *, exclude=()):
    excluded = tuple(exclude)
    try:
        manifest = {}
        for item in storage.list_tree(prefix):
            path = item['path']
            if path.startswith(excluded):
                continue
            if storage._use_s3():
                from .s3_streams import snapshot
                record, version, _ = snapshot(prefix, path, limit=item['size'])
            else:
                target = storage._fs_path(prefix, path)
                version = _identity(target.stat())
                record = file_transactions._capture(target, limit=item['size'], deadline=current())
                if _identity(target.stat()) != version:
                    raise _changed(path)
            if record is None or record['size'] != item['size']:
                raise _changed(path)
            manifest[path] = SourceFile(prefix, path, record['size'], record['sha256'], version)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError) as error:
        raise SourceChanged("读取期间源文件发生变化，请重试") from error
    return manifest


def verify_source(prefix, manifest, *, exclude=()):
    if source_manifest(prefix, exclude=exclude) != manifest:
        raise SourceChanged("读取期间源文件发生变化，请重试")


def remote_versions(prefix, *, exclude=()):
    versions = {}
    for item in storage.list_tree(prefix):
        path = item['path']
        if path.startswith(exclude):
            continue
        if not item['etag']:
            raise _changed(path)
        versions[path] = (item['size'], item['etag'])
    return versions


def verify_remote_versions(prefix, manifest, *, exclude=()):
    """Match the hashed S3 snapshot to the currently published object versions."""
    expected = {path: (entry.size, entry.version.strip('"')) for path, entry in manifest.items()}
    if remote_versions(prefix, exclude=exclude) != expected:
        raise SourceChanged('读取期间源文件发生变化，请重试')


def verify_local_versions(prefix, manifest, *, exclude=()):
    """Recheck an already hashed local snapshot without reopening its bodies."""
    from .attempt_workspace import ordinary
    try:
        versions = {}
        deadline = current()
        for item in storage.list_tree(prefix):
            if deadline is not None:
                deadline.check()
            path = item['path']
            if not path.startswith(exclude):
                versions[path] = _identity(ordinary(storage._fs_path(prefix, path)))
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError) as error:
        raise SourceChanged("读取期间源文件发生变化，请重试") from error
    if versions != {path: entry.version for path, entry in manifest.items()}:
        raise SourceChanged("读取期间源文件发生变化，请重试")
