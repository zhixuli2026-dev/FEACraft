"""Stage an ordinary file change and optionally commit its caller's receipt."""
from contextlib import nullcontext
import re
from types import SimpleNamespace

from .. import storage
from . import case_copy_io, case_read_capacity, file_transactions, local_file_edits, owner_storage_budget
from .async_ops import finish_before_cancel
from .case_provision import io
from .case_write import CaseWriteConflict, coordinate
from .local_file_budget import lock_for_prepared_edit


async def save(db, case, checked, path, build, *, revision, conditions, content_type=None,
               publication=None, on_commit=None, defer_journal=False):
    """Build the new body before taking the storage owner and case write locks."""
    if conditions is not None and any(value is not None and not re.fullmatch('[0-9a-f]{64}', value)
                                      for value in conditions.values()):
        raise ValueError('File precondition must be a SHA-256 or explicit absence')
    if publication is None:
        publication = lambda _db, _case: nullcontext()

    def select(prefix):
        built = build(prefix)
        if conditions is not None:
            return built, conditions
        # Derived edits supply the identity of the version they actually read.
        data, digest = built
        if digest is not None and not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('File precondition must be a SHA-256 or explicit absence')
        return data, {path: digest}

    if storage._use_s3():
        from . import remote_file_edits

        await finish_before_cancel(db.commit())
        return await remote_file_edits.save(checked, path, select, revision=revision,
            content_type=content_type, publication=publication, on_commit=on_commit)

    reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
    owner_id = (await owner_storage_budget.owner_for_case(db, case)).id
    conditional = revision is not None or conditions is None or bool(conditions)

    async def commit():
        await db.commit()
        if on_commit is not None:
            on_commit()

    await finish_before_cancel(db.commit())
    async with case_read_capacity.file_read_deadline(local_file_edits.CODE, local_file_edits.LABEL):
        with case_copy_io.admit(code=local_file_edits.CODE+'_busy', label=local_file_edits.LABEL):
            data, expected = await io(select, reference.s3_prefix)
            async with local_file_edits.prepare_changes(reference, {path:data}) as prepared:
                db.expire_all()
                inventory = await lock_for_prepared_edit(db, owner_id, wait=conditional)
                case = await checked(db)
                if (await owner_storage_budget.owner_for_case(db, case)).id != owner_id:
                    raise CaseWriteConflict('case_source_changed', '项目存储账户已变化，请刷新后重试')
                async with coordinate(db, case, expected_revision=revision, defer_journal=defer_journal):
                    async with publication(db, case):
                        await prepared.publish(db, case, expected, inventory=inventory)
    await finish_before_cancel(commit())
    await file_transactions.finish_committed_edit(db, case, owner_id)
