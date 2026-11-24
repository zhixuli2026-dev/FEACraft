"""Detached selection, body reads, and freshness checks for case results."""

from contextlib import closing
from dataclasses import dataclass
import json
from types import SimpleNamespace

from ..models import CaseEventCounter
from .artifact_publication import PublicationIdentity


async def case_version(db, case):
    counter = await db.get(CaseEventCounter, case.id, populate_existing=True)
    return case.dict_revision, counter.next_seq if counter else None


@dataclass(frozen=True)
class Selection:
    prefix: str
    revision: int
    sequence: int | None
    publication: PublicationIdentity
    job_id: int
    status: str
    current: bool
    main: str | None = None

    @classmethod
    async def capture(cls, db, case, row, active, job_id, *, main=None):
        revision, sequence = await case_version(db, case)
        return cls(case.s3_prefix, revision, sequence,
                   PublicationIdentity.capture(row), job_id, row.status, active is not None and active.id == row.id,
                   main=main)

    def publication_fields(self):
        return {'id': self.publication.id, 'generation': self.publication.generation,
                'status': self.status, 'current': self.current}


async def is_stale(selection, reads):
    from .publication_inputs import resolve
    from .request_deadline import current

    if not selection.current:
        return True
    row = selection.publication
    case = SimpleNamespace(id=row.case_id, s3_prefix=selection.prefix)
    try:
        await resolve(reads, case, [{'publication_id': row.id, 'paths': None, 'require_current': True}],
                      materialize=False)
        stale = False
    except TimeoutError:
        raise
    except (OSError, ValueError, KeyError):
        stale = True
    deadline = current()
    if deadline is not None:
        deadline.check()
    return stale


def archive_json(archive, path):
    with closing(archive.member(path).chunks()) as blocks:
        return json.loads(b''.join(blocks))


async def read_result(db, case_id, user, request, select_result, read_body,
                      *selection_args, code, title):
    from fastapi import HTTPException
    from ..routers.workspaces import get_owned_case
    from . import document_admission
    from .async_ops import finish_before_cancel
    from .case_read_capacity import file_read_deadline, reading
    from .case_write import CaseWriteConflict, bounded_case_read, case_read_slot
    from .file_read import FileReadConflict
    from .publication_read_set import PublicationReadSet
    from .request_lifecycle import wait

    user_id = user.id
    case = await get_owned_case(case_id, user, db, write=False)
    label = f'{title} 结果读取'
    try:
        async with file_read_deadline(code, label):
            async with reading(db, case):
                await document_admission.check_access(db, case, user, write=False)
                selected = await select_result(db, case, *selection_args)
            await finish_before_cancel(db.rollback())

            result = await wait(bounded_case_read(read_body, selected, code=code, label=label), request=request)
            reads = PublicationReadSet()
            if isinstance(selected, Selection):
                with case_read_slot(code, label):
                    result['publication']['stale'] = await wait(is_stale(selected, reads), request=request)
            reads.seal()

            case, user = await document_admission.authorize(db, case_id, user_id, write=False)
            async with reading(db, case):
                await document_admission.check_access(db, case, user, write=False)
                try:
                    current = await select_result(db, case, *selection_args)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                    raise CaseWriteConflict('input_stale', f'{title} 发布在读取期间发生变化，请重试') from error
                if current != selected:
                    raise CaseWriteConflict('input_stale', f'{title} 发布在读取期间发生变化，请重试')
                await reads.require_current(db)
            await finish_before_cancel(db.rollback())
            return result
    except CaseWriteConflict:
        raise
    except (FileReadConflict, OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code':'evidence_invalid','message':f'{title} 发布记录或文件损坏'}) from error
