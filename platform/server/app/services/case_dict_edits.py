"""Plan caseDict edits before settling the dictionary, history and SQL mirror."""
from contextlib import closing, nullcontext
from dataclasses import dataclass
import hashlib
from io import BytesIO
import re

from fastapi import HTTPException

from .. import storage
from ..models import Case
from . import case_copy_io, case_read_capacity, casedict, file_transactions, local_file_edits
from .async_ops import finish_before_cancel
from .case_events import CASE_EVENTS_PATH
from .case_provision import io
from .case_write import CaseWriteConflict, coordinate
from .file_read import FileRead, FileReadConflict
from .owner_storage_budget import owner_for_case
from .provenance import PROVENANCE_PATH
from .storage_admission import lock_owner


PATH = casedict.CASE_DICT_PATH
CODE = 'case_dict_write'
LABEL = 'caseDict 保存'
CONTENT_TYPE = 'text/plain; charset=utf-8'


@dataclass(frozen=True)
class Source:
    id: int
    s3_prefix: str
    dict_revision: int
    workflow_state: str

    @classmethod
    def from_case(cls, case):
        return cls(case.id, case.s3_prefix, case.dict_revision, case.workflow_state)


@dataclass(frozen=True)
class Draft:
    source: Source
    prepared: casedict.PreparedCaseDict | None
    files: dict[str, bytes]
    expected: dict
    receipt: dict
    original_version: object = None
    original_text: str | None = None

    def verify(self, case):
        if Source.from_case(case) != self.source:
            raise _revision_conflict(case.dict_revision)

    async def mirror(self, db, case, _transaction=None):
        if self.prepared is not None:
            self.verify(case)
            casedict._apply_dict_mirror(case, self.prepared)
            await db.flush()


def _revision_conflict(revision):
    return CaseWriteConflict('revision_conflict', 'caseDict 已被其他操作修改，请刷新后重试',
                             current_revision=revision)


def _version(prefix, path):
    if not storage._use_s3():
        return local_file_edits._version(storage._fs_path(prefix, path))
    try:
        return FileRead(prefix, path).version
    except FileNotFoundError:
        return None


def read_member(prefix, path):
    version = _version(prefix, path)
    if version is None:
        return None, None
    try:
        reader = FileRead(prefix, path)
        if reader.size > file_transactions.MAX_BYTES:
            raise ValueError('File transaction object exceeds 1 GiB')
        with BytesIO() as data, closing(reader.chunks()) as chunks:
            for block in chunks:
                data.write(block)
            contents = data.getvalue()
    except FileNotFoundError as error:
        raise FileReadConflict('Case dictionary member changed during the read') from error
    if _version(prefix, path) != version:
        raise FileReadConflict('Case dictionary member changed during the read')
    return contents, version


def _draft(source, edit, expected_sha256, event_source, allow_missing, allow_noop, transition,
           expected_missing, strict_original, actor_id):
    original, version = read_member(source.s3_prefix, PATH)
    old_digest = hashlib.sha256(original).hexdigest() if original is not None else None
    if (expected_sha256 is not None and expected_sha256 != old_digest
            or expected_missing and original is not None):
        raise CaseWriteConflict('file_conflict', '文件内容已变化，请重新读取后再保存',
                                 path=PATH, current_revision=source.dict_revision)
    if original is None and not allow_missing:
        raise FileNotFoundError(PATH)
    text = (original or b'').decode('utf8', 'strict' if transition or strict_original else 'replace')
    updated, details = edit(text, source.s3_prefix)
    prepared = None
    expected = {PATH: old_digest}
    if updated != text or not allow_noop:
        histories = {path: read_member(source.s3_prefix, path)[0] for path in (CASE_EVENTS_PATH, PROVENANCE_PATH)}
        prepared = casedict.prepare_case_dict(source, updated, source=event_source,
                                              existing_files=histories, transition=transition,
                                              check_mesh=transition is None, actor_id=actor_id)
        expected.update(prepared.expected_files)
    receipt = {'ok':True, 'dict_revision':prepared.revision if prepared else source.dict_revision,
               'workflow_state':prepared.workflow_state if prepared else source.workflow_state,
               'content_sha256':hashlib.sha256(updated.encode('utf8')).hexdigest(), **details}
    files = prepared.files if prepared is not None else {}
    return Draft(source, prepared, files, expected, receipt, version, text if transition else None)


def _verify_noop(draft):
    if _version(draft.source.s3_prefix, PATH) != draft.original_version:
        raise FileReadConflict('Case dictionary changed during the edit')


async def _publish_local(db, checked, owner_id, conditional, draft, staged, publication, defer_journal):
    db.expire_all()
    inventory = None
    if staged is None:
        await lock_owner(db, owner_id, wait=conditional)
    else:
        from .local_file_budget import lock_for_prepared_edit
        inventory = await lock_for_prepared_edit(db, owner_id, wait=conditional)
    case = await checked(db)
    if (await owner_for_case(db, case)).id != owner_id:
        raise CaseWriteConflict('case_source_changed', '项目存储账户已变化，请刷新后重试')
    async with coordinate(db, case, expected_revision=draft.source.dict_revision, defer_journal=defer_journal):
        draft.verify(case)
        async with publication(db, case, draft):
            if not draft.files:
                await io(_verify_noop, draft)
            else:
                await staged.publish(db, case, inventory=inventory)
                await draft.mirror(db, case)
    return case


async def _save_remote(checked, revision, build, publication, committed, acknowledge):
    from . import s3_staging_transactions, storage_operations
    from .s3_storage_recovery import recover_reserved

    async with storage_operations.hold(checked, purpose='staging') as lease:
        await recover_reserved(lease)
        async with lease.control() as db:
            case = await db.get(Case, lease.case.id)
            async with coordinate(db, case, expected_revision=revision):
                source = Source.from_case(case)
        draft = await io(build, source)

        def on_commit():
            committed.append((draft.receipt, lease))
            if acknowledge is not None:
                acknowledge(draft.receipt)

        if not draft.files:
            async with lease.control(on_commit=on_commit) as db:
                case = await db.get(Case, lease.case.id)
                draft.verify(case)
                async with publication(db, case, draft):
                    await io(_verify_noop, draft)
        else:
            async def finalize(db, case, transaction):
                draft.verify(case)
                async with publication(db, case, draft):
                    await draft.mirror(db, case, transaction)

            await s3_staging_transactions.write_reserved(lease, draft.files, draft.expected,
                dict.fromkeys(draft.files, CONTENT_TYPE), scope='files', finalize=finalize,
                expected_revision=source.dict_revision, on_commit=on_commit)
        return draft.receipt


async def save(db, case, checked, edit, *, revision, expected_sha256, event_source='api',
               allow_missing=False, allow_noop=False, transition=None, publication=None,
               expected_missing=False, strict_original=False, defer_journal=False, on_commit=None, actor_id=None):
    if expected_sha256 is not None and not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('File precondition must be a SHA-256 or explicit absence')

    def build(source):
        return _draft(source, edit, expected_sha256, event_source, allow_missing, allow_noop, transition,
                      expected_missing, strict_original, actor_id)

    return await publish_draft(db, case, checked, build, revision=revision,
        conditional=revision is not None or expected_sha256 is not None or expected_missing,
        publication=publication, defer_journal=defer_journal, on_commit=on_commit)


async def publish_draft(db, case, checked, build, *, revision, conditional=True, publication=None,
                        defer_journal=False, on_commit=None):
    """Prepare a dictionary or history mutation outside the file commit lock."""
    remote = storage._use_s3()
    owner_id = None if remote else (await owner_for_case(db, case)).id
    source = Source.from_case(case)
    if publication is None:
        publication = lambda _db, _case, _draft: nullcontext()
    await finish_before_cancel(db.commit())
    if remote:
        committed = []
        try:
            async with case_read_capacity.file_read_deadline(CODE, LABEL):
                with case_copy_io.admit(code=CODE+'_busy', label=LABEL):
                    return await _save_remote(checked, revision, build, publication, committed, on_commit)
        except (HTTPException, CaseWriteConflict) as error:
            if not committed:
                raise
            receipt, lease = committed[0]
            # The request deadline can expire while the committed journal drains.
            # Only an acknowledged SQL commit is allowed to override a timeout.
            if not lease.resolved:
                raise HTTPException(409, {'code':'file_cleanup_pending','committed':True,'case_id':source.id,
                    'message':'写入已提交，事务清理仍在等待。请刷新文件版本。'}) from error
            if isinstance(error, HTTPException) and error.status_code==504:
                return receipt
            raise

    async with case_read_capacity.file_read_deadline(CODE, LABEL):
        with case_copy_io.admit(code=CODE+'_busy', label=LABEL):
            if revision is not None and revision != source.dict_revision:
                raise _revision_conflict(source.dict_revision)
            draft = await io(build, source)
            if not draft.files:
                case = await _publish_local(db, checked, owner_id, conditional, draft, None, publication, defer_journal)
            else:
                async with local_file_edits.prepare_changes(source, draft.files, draft.expected) as staged:
                    case = await _publish_local(db, checked, owner_id, conditional, draft, staged, publication, defer_journal)
    async def commit():
        await db.commit()
        if on_commit is not None:
            on_commit(draft.receipt)

    await finish_before_cancel(commit())
    if draft.files:
        await file_transactions.finish_committed_edit(db, case, owner_id)
    return draft.receipt
