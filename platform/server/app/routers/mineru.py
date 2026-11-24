"""Import a case-owned MinerU ZIP using the normal multi-file write journal."""

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import file_transactions, mineru_bundle
from ..services.attempt_workspace import canonical_path
from ..services.case_read_capacity import file_read_deadline, reading_before_action
from ..services.case_write import CaseWriteConflict, bounded_case_read
from ..services.case_write_capacity import committing
from ..services.file_read import FileRead, FileReadConflict
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1/cases/{case_id}/mineru',tags=['mineru'])


class ImportIn(BaseModel):
    path: str = Field(min_length=1,max_length=1000)
    markdown_member: str | None = Field(default=None,max_length=1000)


@router.post(':import')
async def import_bundle(case_id: int, request: ImportIn, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    code, label = 'mineru_import', 'MinerU ZIP 导入'
    try:
        relative = canonical_path(request.path)
        if not relative.lower().endswith('.zip') or relative.split('/')[0].casefold()=='.vibeflow':
            raise HTTPException(422,'请选择算例内的 ZIP 文件')
        async with file_read_deadline(code,label):
            case = await get_owned_case(case_id,user,db)
            async with reading_before_action(db,case):
                archive = await bounded_case_read(FileRead,case.s3_prefix,relative,code=code,label=label)
                if not 0<archive.size<=mineru_bundle.MAX_ARCHIVE:
                    raise HTTPException(413,'ZIP 大小超出限制')
            data = await bounded_case_read(archive.read,code=code,label=label)
            prefix, files, manifest = await bounded_case_read(mineru_bundle.unpack,data,request.markdown_member,code=code,label=label)
            changes = {f'{prefix}/{name}':content for name,content in files.items()}
            content_types = {f'{prefix}/{entry["path"]}':entry['media_type'] for entry in manifest['assets'].values()}
            content_types.update({f'{prefix}/document.md':'text/markdown',f'{prefix}/import.json':'application/json'})
            receipt = f'{prefix}/import.json'
            case = await get_owned_case(case_id,user,db)
            async with committing(db,case,wait_for_quota=True):
                current = await bounded_case_read(FileRead,case.s3_prefix,relative,code=code,label=label)
                if current.version!=archive.version:
                    raise FileReadConflict('ZIP changed before publication')
                reused = await bounded_case_read(storage.exists,case.s3_prefix,receipt,code=code,label=label)
                if reused:
                    await bounded_case_read(_verify_previous,case.s3_prefix,changes,code=code,label=label)
                else:
                    await file_transactions.write(db,case,changes,expected_files=dict.fromkeys(changes),content_types=content_types)
            return {'markdown_path':f'{prefix}/document.md','manifest_path':receipt,'assets':len(manifest['assets']),
                    'source_sha256':manifest['source_sha256'],'reused':reused}
    except FileNotFoundError as error:
        raise HTTPException(404,'找不到 ZIP 源文件') from error
    except FileReadConflict as error:
        raise CaseWriteConflict('input_stale','ZIP 版本已变化，请重新导入',path=request.path) from error
    except mineru_bundle.BundleError as error:
        raise HTTPException(error.status,str(error)) from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict):
            raise
        raise HTTPException(422,str(error)) from error


def _verify_previous(case_prefix, changes):
    for path,content in changes.items():
        try:
            reader = FileRead(case_prefix,path)
            if reader.size==len(content) and hashlib.sha256(reader.read()).digest()==hashlib.sha256(content).digest():
                continue
        except (FileNotFoundError,FileReadConflict):
            pass
        raise CaseWriteConflict('mineru_import_changed','已导入的文件已变化，保留当前版本',path=path)
