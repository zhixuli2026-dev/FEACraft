"""Bounded, UTF-8 aligned reads of actual Job logs; offsets count raw bytes."""
import codecs
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from ..storage import _fs_path, _use_s3
from .file_read import FileRead, FileReadConflict
from .logparse import parse_log_file
from .request_deadline import current


@dataclass(frozen=True)
class Selection:
    job_id: int
    case_id: int
    attempt_id: str | None
    relative: str
    local_path: Path | None
    settled: bool
    status: str


def select(job, attempt, proc, status):
    """Copy authorized state before releasing the DB; no ORM object enters I/O."""
    local_owner = proc is not None and (attempt is None or (proc.owner is not None and proc.owner.attempt_id == attempt.id))
    settled = False
    if status in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
        if local_owner:
            settled = proc.finished
        elif attempt is None or attempt.process_identity_json == '{}':
            settled = True
        else:
            from .process_stop import public
            settled = public(job, attempt) is not None
    return Selection(job.id, job.case_id, attempt.id if attempt else None,
        job.log_s3_key or f'.vibeflow/jobs/job-{job.id}.log',
        proc.log_path if local_owner else None, settled, status)


@dataclass(frozen=True)
class _LiveLog:
    """Read the registered writer's current prefix while new bytes can append."""
    path: Path

    def chunks(self):
        deadline = current()
        if deadline is not None:
            deadline.check()
        selected = self.path.lstat()
        if not stat.S_ISREG(selected.st_mode) or getattr(selected, 'st_file_attributes', 0) & 0x400:
            raise FileReadConflict('Registered job log is no longer a plain file')
        with self.path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            identity = (before.st_dev, before.st_ino)
            if identity != (selected.st_dev, selected.st_ino) or before.st_size < selected.st_size:
                raise FileReadConflict('Registered job log changed before reading')
            remaining = before.st_size
            while remaining:
                if deadline is not None:
                    deadline.check()
                block = stream.read(min(65536, remaining))
                if not block:
                    raise FileReadConflict('Registered job log was truncated during reading')
                remaining -= len(block)
                yield block
            after = os.fstat(stream.fileno())
            chosen = self.path.lstat()
            if ((chosen.st_dev, chosen.st_ino) != identity or after.st_size < before.st_size
                    or (after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns)):
                raise FileReadConflict('Registered job log changed during reading')


def parse(selected, prefix, offset, fingerprint):
    try:
        if selected.local_path is not None and selected.local_path.is_file():
            reader = _LiveLog(selected.local_path)
        else:
            reader = FileRead(prefix, selected.relative)
    except FileNotFoundError:
        return {'available': False}
    try:
        result = parse_log_file(reader, True, offset, fingerprint)
    except FileNotFoundError as error:
        raise FileReadConflict('Registered job log disappeared during reading') from error
    return {'available': True, **result}


def page(selected, prefix, offset, limit, end_offset, *, tail=None):
    deadline = current()
    if deadline is not None:
        deadline.check()
    relative = selected.relative
    path = _fs_path(prefix, relative)
    # The live owner may not have uploaded its immutable S3 log snapshot yet.
    # This path comes only from the authorized Job's real registered process.
    if selected.local_path is not None and selected.local_path.is_file():
        path = selected.local_path
        remote = False
    else:
        remote = _use_s3()
    if remote:
        try:
            reader = FileRead(prefix, relative)
        except FileNotFoundError:
            reader = None
        available = reader is not None
        size = reader.size if available else 0
        if tail is not None:
            offset = max(0, size - tail)
        start = 0 if offset > size else offset
        end = min(size, end_offset) if end_offset is not None else size
        count = max(0, end - start)
        if limit is not None:
            count = min(count, limit)
        # Read only the selected bytes, pinned to the HEAD version. Empty
        # intervals need no GET; FileRead always closes actual response bodies.
        if deadline is not None:
            deadline.check()
        data = reader.read(start, start + count - 1) if count else b''
    elif path.is_file():
        available = True
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if tail is not None:
                offset = max(0, size - tail)
            start = 0 if offset > size else offset
            end = min(size, end_offset) if end_offset is not None else size
            count = max(0, end - start)
            if limit is not None:
                count = min(count, limit)
            handle.seek(start)
            if deadline is not None:
                deadline.check()
            data = handle.read(count)
    else:
        available = False
        size = start = end = 0
        data = b''
    raw_end = start + len(data)
    # A restored tail window can start inside a multi-byte character.
    skip = 0
    if start:
        while skip < len(data) and skip < 3 and data[skip] & 0xc0 == 0x80:
            skip += 1
    start += skip
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    text = decoder.decode(data[skip:], final=(raw_end == size and selected.settled))
    position = raw_end - len(decoder.getstate()[0])
    return {'job_id': selected.job_id, 'case_id': selected.case_id, 'attempt_id': selected.attempt_id,
            'offset': position, 'start_offset': start, 'size': size, 'chunk': text,
            'has_more': raw_end < end, 'reset': offset > size or bool(skip), 'status': selected.status, 'available': available}
