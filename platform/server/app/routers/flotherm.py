"""I01：Flotherm 输入检查与异步 IR→OpenFOAM 转换。"""
from functools import partial
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..raw_download import begin
from ..services import flotherm_library_history as history
from ..services import legacy_job_admission as admission
from ..services.flotherm_import import import_sources, read_source
from ..services.flotherm_inputs import MAX_INPUT_BYTES, inspect_inputs
from ..services.case_read_view import inspect as read_view
from ..services.case_read_capacity import file_read_deadline, reading
from ..services.case_write import CaseWriteConflict, bounded_case_read
from ..services.file_read import FileReadConflict
from .files import _verified_publication_response
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


class PackExportIn(BaseModel):
    model_config = ConfigDict(extra='forbid')

    pack_path: str = Field(min_length=1, max_length=512)
    output_dir: str = Field(min_length=1, max_length=512)


@router.get('/pack-export-capability')
async def pack_export_capability(case_id: int, user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    from ..services.flotherm_pack_handler import configured_launcher
    from ..services.workflow_state import ExecutionConflict
    await get_owned_case(case_id, user, db, write=False)
    try:
        configured_launcher()
    except ExecutionConflict as error:
        return {'available': False, 'message': error.detail}
    return {'available': True, 'message': None}


@router.post(':export-pack')
async def export_pack(case_id: int, body: PackExportIn, request: Request, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db),
                      idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_flotherm(db, case, user, body, idempotency_key,
        'flotherm.export-pack', {'output_dir': _safe(body.output_dir)}, request)


@router.get('/libraries')
async def library_versions(case_id: int, before: UUID | None = None, limit: int = Query(default=20, ge=1, le=50),
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    return await history.list_versions(db, case.id, str(before) if before else None, limit)


@router.get('/libraries/{publication_id}')
async def library_version(case_id: int, publication_id: UUID, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    try:
        async with file_read_deadline('flotherm_library_history', '辅助库版本读取'):
            async with reading(db, case):
                prefix = case.s3_prefix
                row, summary = await history.version(db, case.id, str(publication_id))
            return await bounded_case_read(history.details, prefix, row, summary,
                code='flotherm_library_history', label='辅助库版本读取')
    except CaseWriteConflict:
        raise
    except (FileReadConflict, OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code':'evidence_invalid','message':'辅助库发布记录缺失或损坏'}) from error


def _library_artifact(prefix, row, path, byte_range, version, download):
    response = _verified_publication_response(history.artifact(prefix, row, path), path, byte_range, version, download,
        version_message='辅助库文件版本不一致，请重新读取')
    response.headers['Content-Disposition'] = "attachment; filename*=UTF-8''" + quote(path.rsplit('/', 1)[-1], safe='')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@router.get('/libraries/{publication_id}/artifact')
@router.head('/libraries/{publication_id}/artifact')
async def library_artifact(case_id: int, publication_id: UUID, request: Request,
                           path: str = Query(min_length=1, max_length=512),
                           byte_range: str | None = Header(default=None, alias='Range'),
                           version: str | None = Header(default=None, alias='X-Vibeflow-File-Version',
                                                        pattern=r'^[0-9a-f]{64}$'),
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    download = begin(request) if request.method == 'GET' else None
    try:
        async with file_read_deadline('flotherm_library_artifact', '辅助库原件下载'):
            async with reading(db, case):
                prefix = case.s3_prefix
                row, _ = await history.version(db, case.id, str(publication_id))
            return await bounded_case_read(_library_artifact, prefix, row, path, byte_range, version, download,
                code='flotherm_library_artifact', label='辅助库原件下载')
    except CaseWriteConflict:
        raise
    except (FileReadConflict, OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code':'evidence_invalid','message':'辅助库原件缺失或损坏'}) from error


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
