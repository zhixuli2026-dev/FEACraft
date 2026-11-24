"""Bounded, version-pinned S3 reads for file journals and resumable uploads."""
import hashlib

from botocore.exceptions import ClientError

from .. import storage
from .attempt_objects import arguments
from .case_write import CaseWriteConflict
from .request_deadline import current as current_deadline

HEADERS = ('ContentType', 'CacheControl', 'ContentDisposition', 'ContentEncoding', 'ContentLanguage', 'Metadata')
CHUNK_BYTES = 1024 * 1024


def object_metadata(head):
    metadata = {key: head[key] for key in HEADERS if key in head}
    if not metadata.get('Metadata'):
        metadata.pop('Metadata', None)
    return metadata


def _digest_body(body, size, output=None, deadline=None):
    total, digest = 0, hashlib.sha256()
    while True:
        if deadline is not None:
            deadline.check()
        block = body.read(min(CHUNK_BYTES, size-total+1))
        if not block:
            break
        total += len(block)
        if total > size:
            raise ValueError('S3 body exceeds the declared object size')
        digest.update(block)
        if output is not None:
            output.write(block)
    if total != size:
        raise ValueError('S3 body is incomplete')
    return {'size': total, 'sha256': digest.hexdigest()}


def file_record(prefix, path):
    """Read the size and digest from one GET for an ordinary case path."""
    from ..config import get_settings
    try:
        response = storage.s3().get_object(Bucket=get_settings().s3_bucket, Key=storage._s3_key(prefix, path))
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
            return None
        raise
    body = response['Body']
    try:
        return _digest_body(body, response['ContentLength'], deadline=current_deadline())
    finally:
        body.close()


def snapshot_head(prefix, path, limit):
    try:
        head = storage.s3().head_object(**arguments(prefix, path))
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] == 404:
            return None
        raise
    size, etag = head['ContentLength'], head.get('ETag')
    if not etag or not 0 <= size <= limit:
        raise ValueError('S3 object exceeds its admitted size or has no version')
    return head


def snapshot(prefix, path, *, output=None, limit=1024*1024*1024, rewind=True):
    head = snapshot_head(prefix, path, limit)
    if head is None:
        return None, None, None
    return snapshot_from_head(prefix, path, head, output=output, rewind=rewind)


def snapshot_from_head(prefix, path, head, *, output=None, rewind=True):
    """Read the same object version whose size was used to admit the output."""
    size, etag = head['ContentLength'], head['ETag']
    try:
        response = storage.s3().get_object(**arguments(prefix, path), IfMatch=etag)
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] in {404, 412}:
            raise CaseWriteConflict('input_stale', 'Selected S3 object changed during the read', path=path) from error
        raise
    body = response['Body']
    try:
        if response.get('ETag') != etag or response['ContentLength'] != size:
            raise CaseWriteConflict('input_stale', 'S3 response differs from selected version', path=path)
        record = _digest_body(body, size, output, deadline=current_deadline())
    finally:
        body.close()
    if output is not None:
        output.flush()
        if rewind:
            output.seek(0)
    return record, etag, object_metadata(head)
