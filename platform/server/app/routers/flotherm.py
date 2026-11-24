"""I01：Flotherm 输入检查与异步 IR→OpenFOAM 转换。"""
from functools import partial
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import legacy_job_admission as admission
from ..services.flotherm_import import import_sources, read_source
from ..services.flotherm_inputs import MAX_INPUT_BYTES, inspect_inputs
from ..services.case_read_view import inspect as read_view
from ..services.case_write import CaseWriteConflict, bounded_case_read
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}/flotherm", tags=["flotherm"])
_SCRIPT = Path(__file__).resolve().parent.parent / "services" / "flotherm_convert_job.py"


def _safe(path: str) -> str:
    value = path.replace("\\", "/")
    parsed = PurePosixPath(value)
    if not value or parsed.is_absolute() or PureWindowsPath(value).drive or ".." in parsed.parts:
        raise HTTPException(400, "非法 case 相对路径")
    return parsed.as_posix().rstrip("/")


class InspectIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=8)


class ImportIn(BaseModel):
    path: str | None = None
    paths: list[str] | None = Field(default=None, min_length=1, max_length=3)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_hashes: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] | None = None
    settings_path: str | None = None

    @model_validator(mode='after')
    def selection(self):
        if (self.path is None) == (self.paths is None):
            raise ValueError('请选择一组 FloTHERM 原始输入')
        if self.expected_sha256 is not None and (self.path is None or self.expected_hashes is not None):
            raise ValueError('多个 FloTHERM 输入需要逐文件核对 SHA-256')
        return self


class ConvertIn(BaseModel):
    ir_path: str
    settings_path: str | None = None
    output_dir: str = "C-run/flotherm"

    @model_validator(mode='before')
    @classmethod
    def studio_options(cls, value):
        if isinstance(value, dict) and any(value.get(key) for key in ('force','keep_stage','keep-stage')):
            raise ValueError('Studio 不支持 force/keep-stage，请使用新的空输出目录')
        return value


class LibraryIn(BaseModel):
    source_dir: str
    destination_dir: str = "UDF/flotherm-helper"


@router.post(":build-library")
async def build_library(case_id: int, body: LibraryIn, request: Request, user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db),
                        idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_flotherm(db, case, user, body, idempotency_key,
        'flotherm.build-library', {'source_dir': _safe(body.destination_dir)}, request)


@router.post(":inspect")
async def inspect(case_id: int, body: InspectIn, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    paths = [_safe(path) for path in body.paths]
    prefix = case.s3_prefix

    def check_inputs(root):
        selected = ([root/path for path in paths] if storage._use_s3()
                    else [storage._fs_path(prefix, path) for path in paths])
        return inspect_inputs(selected, studio=True)

    try:
        await db.rollback()
        return await bounded_case_read(partial(read_view, prefix, check_inputs, paths=paths,
                                               max_file_bytes=MAX_INPUT_BYTES),
                                       code='flotherm_inspect', label='FloTHERM 输入核验')
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(400, str(error))


@router.post(":import")
async def import_model(case_id: int, body: ImportIn, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db)
    paths = [_safe(path) for path in (body.paths or [body.path])]
    expected = {paths[0]: body.expected_sha256} if body.expected_sha256 else (body.expected_hashes or {})
    config = _safe(body.settings_path) if body.settings_path else None
    if len(set(paths)) != len(paths) or (expected and set(expected) != set(paths)) or config in paths:
        raise HTTPException(400, '输入文件、预检摘要或配置路径不一致')
    prefix = case.s3_prefix

    def convert_source(root):
        def selected(path):
            return root/path if storage._use_s3() else storage._fs_path(prefix, path)

        inputs = [read_source(selected(path), name=path) for path in paths]
        if expected and any(expected[item.name] != item.sha256 for item in inputs):
            raise CaseWriteConflict('input_stale', 'FloTHERM 文件已变化，请重新预检')
        settings = read_source(selected(config), name=config, limit=1024*1024) if config else None
        model = import_sources(inputs, settings)
        content = json.dumps(model, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
        if len(content.encode('utf-8')) > 4 * 1024 * 1024:
            raise ValueError('生成的 IR 超过 4 MiB')
        return {'content': content, 'coverage': model['coverage'], 'source': model['source']}

    try:
        await db.rollback()
        return await bounded_case_read(partial(read_view, prefix, convert_source, paths=paths+[config] if config else paths,
                                               max_file_bytes=MAX_INPUT_BYTES),
                                       code='flotherm_import', label='FloTHERM 原始文件导入')
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except FileNotFoundError as error:
        raise HTTPException(404, 'FloTHERM 输入文件不存在') from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post(":convert")
async def convert(case_id: int, body: ConvertIn, request: Request, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db),
                  idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_flotherm(db, case, user, body, idempotency_key,
        'flotherm.convert', {'output_dir': _safe(body.output_dir)}, request)


async def _registered_flotherm(db, case, user, body, key, handler, response, request):
    from ..services import registered_input_seal, step_handlers
    from ..services.workflow_state import ExecutionConflict
    try:
        payload = body.model_dump()
        prepare = partial(step_handlers.get_handler(handler).prepare, parameters=payload,
                          budget={'timeout_seconds': 86400, 'max_log_bytes': 16*1024*1024})
        job, created = await registered_input_seal.submit(db, case, user, key, handler, payload, prepare,
                                                          request=request)
        if created:
            try:
                return await admission.launch(db, job, **response)
            except ExecutionConflict as error:
                raise HTTPException(409, {'code': error.code, 'message': error.detail, 'job_id': job.id}) from error
        return await admission.response(db, job, **response)
    except FileExistsError as error:
        raise HTTPException(409, str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(404, 'FloTHERM 输入文件不存在') from error
    except ExecutionConflict as error:
        raise HTTPException(413 if error.code == 'input_too_large' else 409,
                            {'code': error.code, 'message': error.detail}) from error
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
