"""Case-authorized CLI instruction selection, backup and restore."""
from types import SimpleNamespace

from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field,ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from .workspaces import get_owned_case
from ..services import case_copy_io, case_journal_recovery, case_read_capacity, cli_contracts as contracts
from ..services import file_transactions, local_file_edits, owner_storage_budget, s3_staging_transactions, storage_operations
from ..services.case_provision import io
from ..services.case_write import bounded_case_read, coordinate, CaseWriteConflict
from ..services.file_read import FileReadConflict
from ..services.async_ops import finish_before_cancel
from ..services.local_file_budget import lock_for_prepared_edit
from ..services.s3_storage_recovery import recover_reserved

router=APIRouter(prefix='/api/v1/cases/{case_id}/cli-contract',tags=['runtime'])
CODE = 'cli_contract'
LABEL = 'CLI 约定'


class RevisionIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:str=Field(pattern=r'^[0-9a-f]{64}$')


class ApplyIn(RevisionIn):
    harness:contracts.Harness
    instructions:str=Field(default='',max_length=8000)
    provider_profile_id:int|None=Field(default=None,gt=0)


@router.get('')
async def status(case_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    actor = SimpleNamespace(id=user.id)
    async def checked(session):
        return await get_owned_case(case_id, actor, session, write=False)

    try:
        async with case_read_capacity.file_read_deadline(CODE, LABEL):
            case = await checked(db)
            prefix = case.s3_prefix
            if storage._use_s3():
                async with case_read_capacity.reading_before_action(db, case):
                    pass
            else:
                await _prepare_local(db, case, checked)

            state, files, revision, _, selected = await bounded_case_read(contracts.capture, prefix, code=CODE, label=LABEL)
            case = await checked(db)
            async with case_read_capacity.reading(db, case, defer_journal=True):
                await checked(db)
                _verify_source(case, prefix)
                await bounded_case_read(contracts.verify_versions, prefix, selected, code=CODE, label=LABEL)
                return contracts.public(state, files, revision, actor_id=actor.id)
    except CaseWriteConflict as error:raise HTTPException(409,error.detail()) from error
    except FileReadConflict as error:raise HTTPException(409,'配置正在变化，请重新读取') from error
    except (ValueError,UnicodeError) as error:raise HTTPException(422,str(error)) from error
    finally:await finish_before_cancel(db.rollback())


def _verify_source(case, prefix):
    if case.s3_prefix != prefix:
        raise CaseWriteConflict('case_source_changed', '算例来源已变化，请重新读取')


async def _prepare_local(db, case, checked):
    pending = case_journal_recovery.pending_local(case)
    await finish_before_cancel(db.commit())
    if pending is not None:
        await case_journal_recovery.recover_local(checked, pending)


async def mutate(case_id,user,db,body,*,restore_id=None):
    actor = SimpleNamespace(id=user.id)

    async def checked(session):
        return await get_owned_case(case_id, actor, session, write=True)

    def draft(prefix):
        state, files, revision, expected, _ = contracts.capture(prefix)
        if revision != body.expected_revision:
            raise HTTPException(409, '配置已变化，请重新读取后再提交')
        changes, backup = contracts.change(state, files, actor.id, restore_id=restore_id,
            selected=body.harness if isinstance(body, ApplyIn) else None,
            instructions=body.instructions if isinstance(body, ApplyIn) else '',
            profile_id=body.provider_profile_id if isinstance(body, ApplyIn) else None)
        return changes, expected, backup

    try:
        case = await checked(db)
        if isinstance(body, ApplyIn) and body.provider_profile_id is not None:
            from ..services.terminal_credentials import descriptor, owned
            if body.harness not in {'codex','opencode','kimi'}:
                raise HTTPException(422, '此原生工具不支持绑定模型配置')
            descriptor(await owned(db, body.provider_profile_id, actor.id), body.harness)
        if storage._use_s3():
            await finish_before_cancel(db.commit())
            async with case_read_capacity.file_read_deadline(CODE, LABEL):
                with case_copy_io.admit(code=CODE+'_busy', label=LABEL):
                    async with storage_operations.hold(checked, purpose='staging') as lease:
                        await recover_reserved(lease)
                        changes, expected, backup = await io(draft, lease.case.s3_prefix)
                        await s3_staging_transactions.write_reserved(lease, changes, expected, scope='files')
        else:
            reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
            owner_id = (await owner_storage_budget.owner_for_case(db, case)).id
            async with case_read_capacity.file_read_deadline(CODE, LABEL):
                await _prepare_local(db, case, checked)
                with case_copy_io.admit(code=CODE+'_busy', label=LABEL):
                    changes, expected, backup = await io(draft, reference.s3_prefix)
                    async with local_file_edits.prepare_changes(reference, changes, expected) as prepared:
                        db.expire_all()
                        inventory = await lock_for_prepared_edit(db, owner_id, wait=True)
                        case = await checked(db)
                        async with coordinate(db, case, defer_journal=True):
                            await checked(db)
                            _verify_source(case, reference.s3_prefix)
                            if (await owner_storage_budget.owner_for_case(db, case)).id != owner_id:
                                raise CaseWriteConflict('case_source_changed', '算例存储账户已变化，请重新读取')
                            await prepared.publish(db, case, inventory=inventory)
                    await finish_before_cancel(db.commit())
            await file_transactions.finish_committed_edit(db, case, owner_id)
        return {'ok':True,'backup_id':backup}
    except CaseWriteConflict as error:raise HTTPException(409,error.detail()) from error
    except FileReadConflict as error:raise HTTPException(409,'配置正在变化，请重新读取') from error
    except KeyError as error:raise HTTPException(404,'备份不存在') from error
    except (ValueError,UnicodeError) as error:raise HTTPException(422,str(error)) from error
    finally:await finish_before_cancel(db.rollback())


@router.post('')
async def apply(case_id:int,body:ApplyIn,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    return await mutate(case_id,user,db,body)


@router.post('/backups/{backup_id}:restore')
async def restore(case_id:int,backup_id:str,body:RevisionIn,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    return await mutate(case_id,user,db,body,restore_id=backup_id)
