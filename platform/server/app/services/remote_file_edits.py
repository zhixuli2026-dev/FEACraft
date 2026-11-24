"""Ordinary S3 edits use the case reservation and per-object storage claims."""
from ..models import Case
from . import case_copy_io, case_read_capacity, s3_staging_transactions, storage_operations
from .case_write import CaseWriteConflict, coordinate
from .case_provision import io
from .s3_storage_recovery import recover_reserved


async def save(checked, path, select, *, revision, content_type, publication, on_commit=None):
    async with case_read_capacity.file_read_deadline('file_write', '文件保存'):
        with case_copy_io.admit(code='file_write_busy', label='文件保存'):
            async with storage_operations.hold(checked, purpose='staging') as lease:
                await recover_reserved(lease)
                async with lease.control() as db:
                    case = await db.get(Case, lease.case.id)
                    async with coordinate(db, case, expected_revision=revision) as snapshot:
                        current_revision = snapshot.dict_revision
                data, conditions = await io(select, lease.case.s3_prefix)

                async def finalize(db, case, _transaction):
                    async with publication(db, case):
                        pass

                try:
                    await s3_staging_transactions.write_reserved(
                        lease, {path: data}, conditions, {path: content_type} if content_type else None,
                        scope='files', expected_revision=revision, finalize=finalize, on_commit=on_commit)
                except CaseWriteConflict as error:
                    if error.code == 'file_conflict' and error.current_revision is None:
                        error.current_revision = current_revision
                    raise
