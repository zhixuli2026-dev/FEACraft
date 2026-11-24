"""Pin one object's identity while assembling a raw download response.

This does not pin a multi-file publication. Publication consumers must continue
to resolve a publication ID and verify its manifest through publication_inputs.
"""
import codecs
import hashlib
import io
import json
import os
import stat
from contextlib import closing, contextmanager

from .. import storage
from ..config import get_settings


class FileReadConflict(Exception):
    """The selected object changed, or its identity could not be verified."""


class PublicationRead:
    """Ranges over fully verified immutable bytes, bound to their publication.

    The existing publication reader verifies the manifest and complete digest.
    This bounds HTTP responses, not backend IO: no partial hash is treated as
    proof of a whole artifact, including on S3.
    """
    def __init__(self, prefix, row, path):
        from .artifact_publication import read_file
        self.data = read_file(prefix, row, path)
        self.size = len(self.data)
        self.version = hashlib.sha256(json.dumps(
            [row.id, row.manifest_sha256, path, hashlib.sha256(self.data).hexdigest()],
            separators=(',', ':')).encode('utf8')).hexdigest()

    def read(self, start=None, end=None):
        if start is None:
            return self.data
        if end is None or not 0 <= start <= end < self.size:
            raise ValueError('Invalid read range')
        return self.data[start:end + 1]


class ArchivedPublication:
    """Read an owned archived bundle without hydrating a local S3 cache."""
    def __init__(self, prefix, row):
        self.prefix, self.row = prefix, row
        if storage._use_s3():
            from .publication_objects import _bound_snapshot
            self.snapshot = _bound_snapshot(prefix, row)
            self.manifest = self.snapshot[0]
            self.folder = None
        else:
            from .artifact_publication import read_manifest
            self.folder, self.manifest = read_manifest(prefix, row)

    def member(self, path):
        record = self.manifest['files'].get(path)
        if record is None:
            raise FileNotFoundError(path)
        if self.folder is not None:
            from .attempt_workspace import checked_path
            checked_path(self.folder / 'files', path)
        return ArchivedMemberRead(self.prefix, self.row, path, record)

    def verify_other_than(self, excluded):
        records = {path:record for path,record in self.manifest['files'].items() if path not in excluded}
        if self.folder is None:
            from .attempt_objects import verify_members
            verify_members(self.prefix, self.row.immutable_path + '/files', records)
        else:
            for path in records:
                with closing(self.member(path).chunks()) as chunks:
                    for _ in chunks:
                        pass

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        if kind is not None:
            return
        if self.folder is None:
            from .publication_objects import _bound_snapshot
            if _bound_snapshot(self.prefix,self.row) != self.snapshot:
                raise FileReadConflict('Publication objects changed during download preparation')
        else:
            from .artifact_publication import read_manifest
            if read_manifest(self.prefix,self.row)[1] != self.manifest:
                raise FileReadConflict('Publication manifest changed during download preparation')


class ArchivedMemberRead:
    """One full digest proof over the selected immutable object's stream."""
    def __init__(self, prefix, row, path, record):
        self.prefix = prefix
        self.path = row.immutable_path + '/files/' + path
        self.file = FileRead(prefix, self.path)
        self.size = record['size']
        self.sha256 = record['sha256']
        if self.file.size != self.size:
            raise FileReadConflict('Published member length differs from its manifest')
        self.version = hashlib.sha256(json.dumps(
            [row.id, row.manifest_sha256, path, self.sha256],
            separators=(',', ':')).encode('utf8')).hexdigest()

    def chunks(self):
        from .attempt_objects import selected_stream

        if self.file.remote and not self.size:
            # Preserve the legacy SeaweedFS empty-object conditional fallback.
            source = selected_stream(self.prefix, self.path, etag=self.file.etag, size=0)
        else:
            source = closing(self.file.chunks())
        digest, size = hashlib.sha256(), 0
        with source as blocks:
            for block in blocks:
                size += len(block)
                digest.update(block)
                yield block
        if (size != self.size or digest.hexdigest() != self.sha256
                or FileRead(self.prefix, self.path).version != self.file.version):
            raise FileReadConflict('Published member differs from its manifest')


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _remote_error(error, *, selected):
    response = error.response
    code = str(response.get('Error', {}).get('Code', ''))
    status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
    if code in {'NoSuchKey', 'NotFound', '404'} or (not code and status == 404):
        if selected:
            raise FileReadConflict('Selected object was removed') from error
        raise FileNotFoundError('File does not exist') from error
    if code in {'PreconditionFailed', '412'} or (not code and status == 412):
        raise FileReadConflict('Selected object was replaced') from error
    raise error


class FileRead:
    @classmethod
    def from_version(cls, client, bucket, key, etag, size):
        """Read a previously selected immutable or legacy object version."""
        if type(size) is not int or size < 0 or not isinstance(etag, str) or not etag:
            raise FileReadConflict('Object listing did not supply a read precondition')
        reader = cls.__new__(cls)
        reader.remote, reader.client = True, client
        reader.location, reader.etag, reader.size = dict(Bucket=bucket, Key=key), etag, size
        return reader

    def __init__(self, prefix, path):
        self.remote = storage._use_s3()
        if self.remote:
            from botocore.exceptions import ClientError
            self.client = storage.s3()
            self.location = dict(Bucket=get_settings().s3_bucket, Key=storage._s3_key(prefix, path))
            try:
                metadata = self.client.head_object(**self.location)
            except ClientError as error:
                _remote_error(error, selected=False)
            self.size = int(metadata['ContentLength'])
            modified = metadata.get('LastModified')
            self.mtime = int(modified.timestamp()) if modified is not None else 0
            self.etag = metadata.get('ETag')
            if not isinstance(self.etag, str) or not self.etag:
                raise FileReadConflict('Object store did not supply a read precondition')
            identity = ['s3', get_settings().s3_endpoint, self.location, self.etag, self.size]
        else:
            self.path = storage._fs_path(prefix, path)
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise FileNotFoundError('Path is not a regular file')
            self.identity = _identity(info)
            self.size = info.st_size
            self.mtime = int(info.st_mtime)
            identity = ['fs', str(self.path), self.identity]
        # Opaque object identity, not a claimed content digest or HTTP strong ETag.
        # It is stable across API restarts and binds the storage location as well
        # as the version. A range read still validates this identity during I/O.
        self.version = hashlib.sha256(json.dumps(identity, sort_keys=True,
            separators=(',', ':')).encode('utf8')).hexdigest()

    @contextmanager
    def _open(self, start=None, end=None):
        if start is None:
            expected = self.size
        else:
            if end is None or not 0 <= start <= end < self.size:
                raise ValueError('Invalid read range')
            expected = end - start + 1
        if self.remote:
            from botocore.exceptions import ClientError
            params = {**self.location, 'IfMatch': self.etag}
            if start is not None:
                params['Range'] = f'bytes={start}-{end}'
            try:
                response = self.client.get_object(**params)
            except ClientError as error:
                _remote_error(error, selected=True)
            body = response['Body']
            try:
                # Check the response as well: an incompatible store must not
                # silently ignore If-Match or return an entire object for Range.
                if response.get('ETag') != self.etag or int(response['ContentLength']) != expected:
                    raise FileReadConflict('Object response differs from the selected version')
                if start is not None and response.get('ContentRange') != f'bytes {start}-{end}/{self.size}':
                    raise FileReadConflict('Object store did not honor the requested range')
                yield body, expected
            finally:
                body.close()
        else:
            try:
                with self.path.open('rb') as stream:
                    if _identity(os.fstat(stream.fileno())) != self.identity:
                        raise FileReadConflict('Selected file was replaced or modified')
                    if start is not None:
                        stream.seek(start)
                    yield stream, expected
                    if _identity(os.fstat(stream.fileno())) != self.identity:
                        raise FileReadConflict('Selected file changed during the read')
            except (FileNotFoundError, IsADirectoryError) as error:
                raise FileReadConflict('Selected file was removed or replaced') from error
    def read(self, start=None, end=None):
        """Read all bytes or an inclusive range without loading unrelated bytes."""
        with self._open(start, end) as (stream, expected):
            data = stream.read(expected)
            if len(data) != expected:
                raise FileReadConflict('Selected file returned an incomplete body')
            return data

    def chunks(self, start=None, end=None):
        """Read one pinned version in bounded blocks using a single open handle."""
        from .request_deadline import current
        deadline = current()
        if deadline is not None:
            deadline.check()

        with self._open(start, end) as (stream, remaining):
            while remaining:
                if deadline is not None:
                    deadline.check()
                block = stream.read(min(64 * 1024, remaining))
                if not block or len(block) > remaining:
                    raise FileReadConflict('Selected file returned an incomplete body')
                remaining -= len(block)
                yield block
        if deadline is not None:
            deadline.check()

    def text_snapshot(self):
        """Decode and hash one pinned stream, including split UTF-8 codepoints."""
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        digest = hashlib.sha256()
        with io.StringIO() as text:
            for block in self.chunks():
                digest.update(block)
                text.write(decoder.decode(block))
            text.write(decoder.decode(b'', final=True))
            return text.getvalue(), digest.hexdigest()
