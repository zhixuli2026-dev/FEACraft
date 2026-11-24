"""几何服务路由：CadQuery 预览/导出/只读 STEP 导入（走作业系统执行，场景文件进存储）
"""
from contextlib import asynccontextmanager, contextmanager
from functools import partial
import hashlib
import json
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.case_write_capacity import committing
from ..db import get_db
from ..deps import get_current_user
from ..models import ArtifactPublication, JobAttempt, User
from ..services import job_execution, artifact_publication, geometry_publication, step_handlers, file_transactions, geometry_conversion
from ..services import geometry_submission, request_lifecycle
from ..services.geo_script import DEFAULT_TOLERANCE, _tessellate
from ..services.geo_repair import repair_report
from ..services.file_read import FileRead, FileReadConflict
from ..storage import _fs_path, exists, get_bytes
from ..services.case_write import CaseWriteConflict, bounded_case_read
from ..services.case_read_capacity import file_read_deadline, reading, reading_before_action
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}/geometry", tags=["geometry"])


async def _launch(case_id: int, mode: str, user: User, db: AsyncSession, idempotency_key=None, *,
                  tolerance=DEFAULT_TOLERANCE, saved_source_sha256=None, request=None):
    try:
        job_execution.validate_key(idempotency_key)
        return await _launch_owned(case_id, mode, user, db, idempotency_key, tolerance, saved_source_sha256, request)
    except job_execution.ExecutionConflict as error:
        raise HTTPException(409, {'code':error.code, 'message':error.detail}) from error
    except ValueError as error:
        if isinstance(error, CaseWriteConflict):
            raise
        raise HTTPException(400, str(error)) from error


async def _launch_owned(case_id: int, mode: str, user: User, db: AsyncSession, idempotency_key, tolerance,
                        saved_source_sha256, request):
    parameters = {'tolerance': tolerance} if tolerance != DEFAULT_TOLERANCE else {}
    digest = job_execution.request_hash({'handler':'geometry.v1', 'mode':mode, **parameters})
    try:
        job, publication = await request_lifecycle.wait(geometry_submission.submit(db,case_id,user.id,mode,
            idempotency_key,digest,parameters,saved_source_sha256),request=request)
    except (CaseWriteConflict, HTTPException) as error:
        # File save is durable even if the follow-up input admission is superseded.
        if saved_source_sha256 is not None:
            if isinstance(error, CaseWriteConflict) and (error.code=='input_stale' or
                    (error.code=='file_conflict' and error.path==geometry_submission.SCRIPT)):
                return {'job_id': None, 'auto_preview_error': {'code': 'stale_case_files',
                        'message': '几何输入已有更新，已跳过本次保存的自动预览。'}}
            if (isinstance(error, HTTPException) and error.status_code == 409
                    and isinstance(error.detail, dict) and error.detail.get('code') == 'storage_quota_busy'):
                return {'job_id': None, 'auto_preview_error': {'code': error.detail['code'],
                        'message': error.detail['message']}}
        raise
    if publication is None:
        return {'job_id':job.id, 'status':job.status}
    proc = await step_handlers.launch(job.id)
    return {'job_id':job.id, 'status':proc.status, 'publication_id':publication.id, 'generation':publication.generation}


class PreviewIn(BaseModel):
    tolerance: float = Field(default=DEFAULT_TOLERANCE, strict=True, gt=0, allow_inf_nan=False)


@router.post(":preview")
async def preview(case_id: int, request: Request, body: PreviewIn | None = None, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db),
                  idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    """执行 A-geo/geometry.py → 生成 scene.json。返回作业 id（轮询/SSE 跟日志）。"""
    return await _launch(case_id, "preview", user, db, idempotency_key,
                         tolerance=body.tolerance if body is not None else DEFAULT_TOLERANCE, request=request)


@router.post(":export")
async def export(case_id: int, request: Request, user: User = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db),
                 idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    """导出 STEP/STL + manifest 到 A-geo/exports/"""
    return await _launch(case_id, "export", user, db, idempotency_key, request=request)


@asynccontextmanager
async def _geometry_source(db, case_id, user, path, suffixes, label, *, write, absent=()):
    if PurePosixPath(path).suffix.lower() not in suffixes:
        raise HTTPException(400, '只支持 ' + '/'.join(suffix.upper().strip('.') for suffix in suffixes))
    with geometry_conversion.admission():
        async with file_read_deadline('geometry_conversion', label):
            case = await get_owned_case(case_id, user, db, write=write)
            try:
                async with reading_before_action(db, case):
                    reader = await bounded_case_read(FileRead, case.s3_prefix, path)
                    if reader.size > geometry_conversion.INPUT_LIMIT:
                        raise HTTPException(413, label + '不能超过 16 MiB')
                    if absent and await bounded_case_read(_repair_target_exists, case.s3_prefix, absent):
                        raise HTTPException(409, '修复产物已存在，显式 overwrite=true 才可覆盖')
            except FileNotFoundError as error:
                raise HTTPException(404, '源文件不存在') from error
            except FileReadConflict as error:
                raise CaseWriteConflict('input_stale', '源文件版本无法核验，请重新读取', path=path) from error
            except CaseWriteConflict:
                raise
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            try:
                yield case.s3_prefix, reader
            except FileReadConflict as error:
                raise CaseWriteConflict('input_stale', '源文件在几何转换期间发生变化，请重新读取', path=path) from error


def _repair_target_exists(prefix, paths):
    return any(exists(prefix, path) for path in paths)


async def _verify_geometry_source(case, path, reader):
    try:
        current = await bounded_case_read(FileRead, case.s3_prefix, path)
    except FileNotFoundError as error:
        raise FileReadConflict('Selected source was removed') from error
    if current.version != reader.version:
        raise FileReadConflict('Selected source was replaced')


async def _publish_geometry(db, case_id, user, path, reader, changes, types, *, absent=()):
    case = await get_owned_case(case_id, user, db)
    expected = dict.fromkeys(absent)
    async with committing(db, case, expected_files=expected, wait_for_quota=True):
        await get_owned_case(case_id, user, db)
        await _verify_geometry_source(case, path, reader)
        await file_transactions.write(db, case, changes, content_types=types, expected_files=expected)


def _repair_step(source, operation, axis, period, count, factor):
    import cadquery as cq

    repaired = repair_report(cq.importers.importStep(str(source)), operation, axis, period, count, factor)
    geometry_conversion.check_time()
    output = source.with_name('repaired.step')
    cq.exporters.export(repaired['shape'], str(output))
    return geometry_conversion.output_bytes(output), repaired['validation']


@router.post('/repair')
async def repair_geometry(case_id: int, body: dict, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    path = str(body.get('path', ''))
    operation = str(body.get('operation', 'sew'))
    if operation not in {'sew', 'fix', 'mirror', 'periodic', 'scale'}:
        raise HTTPException(400, '未知几何修复操作')
    axis = str(body.get('axis', 'XY'))
    try:
        period, count = float(body.get('period', 1)), int(body.get('count', 2))
    except (TypeError, ValueError, OverflowError) as error:
        raise HTTPException(400, '无效的几何修复参数') from error
    factor = body.get('factor', 1.0)
    target = f'A-geo/exports/repaired-{operation}.step'
    validation_path = f'A-geo/exports/repaired-{operation}-validation.json'
    absent = () if body.get('overwrite') is True else (target, validation_path)
    async with _geometry_source(db, case_id, user, path, ('.step', '.stp'), '几何修复', write=True, absent=absent) as (_, reader):
        try:
            convert = partial(_repair_step, operation=operation, axis=axis, period=period, count=count, factor=factor)
            (step, validation), digest, _ = await geometry_conversion.prepare(reader, path, convert, produces_file=True)
        except (FileReadConflict, CaseWriteConflict, TimeoutError):
            raise
        except Exception as error:
            raise HTTPException(400, f'几何修复失败：{type(error).__name__}: {error}') from error
        validation_document = json.dumps({
            'schema': 'vibeflow.geometry.validation.v1', 'sourcePath': path,
            'sourceSha256': digest, 'operation': operation, 'axis': axis, 'period': period,
            'count': count, 'factor': factor, 'objects': [validation], 'summary': {
                'valid': validation['valid'], 'watertight': validation['watertight'],
                'manifold': validation['manifold'], 'object_count': 1,
            }}, ensure_ascii=False, indent=1).encode('utf8')
        await _publish_geometry(db, case_id, user, path, reader, {target: step, validation_path: validation_document},
                                {target: 'application/step', validation_path: 'application/json'}, absent=absent)
    return {'step': target, 'validation': validation_path, 'result': validation}


def _convert_gmsh(source):
    import meshio
    from vtkmodules.vtkCommonCore import vtkIdTypeArray, vtkPoints, vtkUnsignedCharArray
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkUnstructuredGrid
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridWriter

    try:
        mesh = meshio.read(source)
    except SystemExit as error:
        raise ValueError('Gmsh 文件无法读取') from error
    point_count, cell_count = len(mesh.points), sum(len(block.data) for block in mesh.cells)
    if point_count > 500_000 or cell_count > 1_000_000:
        raise ValueError('模型超过 50 万点/100 万单元预览上限')
    grid = vtkUnstructuredGrid()
    points = vtkPoints()
    points.SetNumberOfPoints(point_count)
    for index, point in enumerate(mesh.points):
        if index % 4096 == 0:
            geometry_conversion.check_time()
        xyz = list(point) + [0.0] * (3 - len(point))
        points.SetPoint(index, *[float(value) for value in xyz[:3]])
    grid.SetPoints(points)
    cells, types, locations = vtkCellArray(), vtkUnsignedCharArray(), vtkIdTypeArray()
    cell_types = {'vertex': 1, 'line': 3, 'triangle': 5, 'quad': 9,
                  'tetra': 10, 'hexahedron': 12, 'wedge': 13, 'pyramid': 14}
    offset = 0
    for block in mesh.cells:
        if block.type not in cell_types:
            raise ValueError(f'暂不支持的 Gmsh 单元类型: {block.type}')
        for index, cell in enumerate(block.data):
            if index % 4096 == 0:
                geometry_conversion.check_time()
            cells.InsertNextCell(len(cell), [int(value) for value in cell])
            types.InsertNextValue(cell_types[block.type])
            locations.InsertNextValue(offset)
            offset += len(cell) + 1
    grid.SetCells(types, locations, cells)
    output = source.with_name('preview.vtu')
    writer = vtkXMLUnstructuredGridWriter()
    writer.SetFileName(str(output))
    writer.SetInputData(grid)
    if writer.Write() != 1:
        raise ValueError('VTU 写入失败')
    return geometry_conversion.output_bytes(output), point_count, cell_count


@router.post('/msh-preview')
async def preview_gmsh(case_id: int, body: dict, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    path = str(body.get('path', ''))
    async with _geometry_source(db, case_id, user, path, ('.msh',), 'Gmsh 预览', write=True) as (_, reader):
        try:
            (vtu, points, cells), digest, address = await geometry_conversion.prepare(reader, path, _convert_gmsh, produces_file=True)
        except (FileReadConflict, CaseWriteConflict, TimeoutError):
            raise
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except Exception as error:
            raise HTTPException(400, f'Gmsh 导入失败：{type(error).__name__}: {error}') from error
        target = f'.vibeflow/msh-preview/{address[:20]}/preview.vtu'
        await _publish_geometry(db, case_id, user, path, reader, {target: vtu},
                                {target: 'application/vnd.vtk.unstructuredgrid+xml'})
    return {'path': target, 'source_path': path, 'source_sha256': digest, 'points': points, 'cells': cells}


def _import_step(source, name):
    import cadquery as cq

    shape = cq.importers.importStep(str(source))
    return _tessellate(name, shape, '#2563eb', 1.0, 0.1, limits=(500_000, 1_000_000))


@router.get('/import-scene')
async def import_step_scene(case_id: int, path: str, user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    async with _geometry_source(db, case_id, user, path, ('.step', '.stp'), 'STEP 预览', write=False) as (prefix, reader):
        try:
            convert = partial(_import_step, name=PurePosixPath(path).name)
            mesh, digest, _ = await geometry_conversion.prepare(reader, path, convert)
        except (FileReadConflict, CaseWriteConflict, TimeoutError):
            raise
        except ValueError as error:
            raise HTTPException(413, str(error)) from error
        except Exception as error:
            raise HTTPException(400, f'STEP 导入失败：{type(error).__name__}: {error}') from error
        case = await get_owned_case(case_id, user, db, write=False)
        async with reading_before_action(db, case):
            await _verify_geometry_source(case, path, reader)
    return {'schema': 'vibeflow.cadquery.scene.v1', 'caseRoot': prefix, 'sourcePath': path,
            'sourceSha256': digest, 'objects': [mesh], 'messages': []}


@router.get("/validation")
async def get_validation(case_id: int, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db), publication_id: str | None = None):
    return await _document(case_id, user, db, 'A-geo/validation.json', publication_id)


@router.get("/scene")
async def get_scene(case_id: int, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db), publication_id: str | None = None):
    """读取最新 scene.json（preview 作业完成后调用）"""
    return await _document(case_id, user, db, 'A-geo/scene.json', publication_id)


async def _document(case_id, user, db, path, publication_id):
    case = await get_owned_case(case_id, user, db, write=False)
    async with reading(db, case):
        head, active = await artifact_publication.current(db, case.id, 'geometry')
        row = active
        if publication_id is not None:
            row = await db.get(ArtifactPublication, publication_id)
            if row is None or row.case_id != case.id or row.kind != 'geometry' or row.manifest_sha256 == '0'*64:
                raise HTTPException(404, '几何版本不存在')
        if row is None:
            if head is not None and path.endswith('validation.json'):
                raise HTTPException(409, '当前几何成果未发布或已被手工修改，请重新预览')
            if not exists(case.s3_prefix, path):
                raise HTTPException(404, '几何文件不存在，请先运行预览')
            result = json.loads(get_bytes(case.s3_prefix, path))
            if head is not None:
                result['publication'] = {'status':'draft', 'stale':True}
        else:
            try:
                result = json.loads(artifact_publication.read_file(case.s3_prefix, row, path))
                _, manifest = artifact_publication.read_manifest(case.s3_prefix, row)
                stale = geometry_publication.source_snapshot(case.s3_prefix,
                    manifest['descriptor'].get('generated_paths', [])) != manifest['descriptor']['source']
            except (OSError, ValueError, KeyError) as error:
                raise HTTPException(409, '几何版本文件损坏或输入不可读') from error
            result['publication'] = {'id':row.id, 'generation':row.generation, 'status':row.status,
                'current':active is not None and active.id == row.id, 'stale':stale}
    await db.commit()
    return result


@router.get('/publications')
async def publications(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    async with reading(db, case):
        _, active = await artifact_publication.current(db, case.id, 'geometry')
        rows = list(await db.scalars(select(ArtifactPublication).where(
            ArtifactPublication.case_id == case.id, ArtifactPublication.kind == 'geometry')
            .order_by(ArtifactPublication.generation.desc()).limit(50)))
        result = [{'id':row.id, 'generation':row.generation, 'status':row.status, 'attempt_id':row.attempt_id,
            'current':active is not None and active.id == row.id, 'path':row.immutable_path,
            'manifest_sha256':row.manifest_sha256} for row in rows]
    await db.commit()
    return result


async def _published_exports(db, case, publication_id):
    """Resolve immutable export bytes; live aliases never replace this selection."""
    from ..services.publication_inputs import resolve
    try:
        _, active = await artifact_publication.current(db, case.id, 'geometry')
        row = await db.get(ArtifactPublication, publication_id) if publication_id else active
        if row is None or row.case_id != case.id or row.kind != 'geometry':
            raise HTTPException(404, '几何发布不存在，请先完成几何导出')
        await resolve(db, case, [{'publication_id': row.id, 'paths': None, 'require_current': False}])
        _, manifest = artifact_publication.read_manifest(case.s3_prefix, row)
        try:
            await resolve(db, case, [{'publication_id': row.id, 'paths': None, 'require_current': True}])
            stale = False
        except (ValueError, OSError, KeyError):
            stale = True
        files = [{'path': path, 'size': record['size'], 'sha256': record['sha256']}
                 for path, record in sorted(manifest['files'].items())
                 if _export_path(path)]
        return row, {'publication': {'id': row.id, 'generation': row.generation, 'status': row.status,
                     'current': active is not None and active.id == row.id, 'stale': stale}, 'files': files}
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code': 'evidence_invalid', 'message': '几何发布记录或文件无法核验，请核对原版本'}) from error


@router.get('/results')
async def published_results(case_id: int, publication_id: str | None = None,
                            user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=False)
    async with reading(db, case):
        _, result = await _published_exports(db, case, publication_id)
        return result


@router.get('/artifact')
@router.head('/artifact')
async def published_artifact(case_id: int, publication_id: str, path: str, request: Request,
                             range_header: str | None = Header(default=None, alias='Range'),
                             file_version: str | None = Header(default=None, alias='X-Vibeflow-File-Version', pattern=r'^[0-9a-f]{64}$'),
                             user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..models import Job
    from ..raw_download import begin
    from ..services.async_ops import finish_before_cancel

    case = await get_owned_case(case_id, user, db, write=False)
    download = begin(request) if request.method == 'GET' else None
    try:
        async with file_read_deadline('geometry_artifact','几何原件读取'):
            async with reading(db, case):
                prefix = case.s3_prefix
                await artifact_publication.current(db,case.id,'geometry')
                row = await db.get(ArtifactPublication,publication_id)
                if row is None or row.case_id != case.id or row.kind != 'geometry':
                    raise HTTPException(404,'几何发布不存在，请先完成几何导出')
                job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
                                      .where(JobAttempt.id == row.attempt_id))
                if (row.status not in {'committed','superseded'} or job is None
                        or job.case_id != case.id or job.status != 'succeeded'):
                    raise ValueError('Geometry publication has no successful owned Job')
                selected = artifact_publication.PublicationIdentity.capture(row)
            await finish_before_cancel(db.rollback())
            return await bounded_case_read(_geometry_artifact_response,prefix,selected,path,range_header,
                file_version,download,code='geometry_artifact',label='几何原件读取')
    except CaseWriteConflict:
        raise
    except (FileReadConflict,OSError,ValueError,KeyError,TypeError) as error:
        raise HTTPException(409,{'code':'evidence_invalid','message':'所选几何原件无法核验'}) from error


def _export_path(path):
    return path.startswith('A-geo/exports/') and PurePosixPath(path).suffix.lower() in {'.step','.stp','.stl'}


@contextmanager
def _geometry_artifact_reader(prefix,row,path):
    from ..services.file_read import ArchivedPublication

    with ArchivedPublication(prefix,row) as archive:
        allowed = _export_path(path) and path in archive.manifest['files']
        archive.verify_other_than({path} if allowed else set())
        if not allowed:
            raise HTTPException(404,'所选几何版本不包含此导出文件')
        yield archive.member(path)


def _geometry_artifact_response(prefix,row,path,range_header,file_version,download):
    from .files import _verified_publication_response

    return _verified_publication_response(_geometry_artifact_reader(prefix,row,path),path,range_header,
        file_version,download,version_message='几何下载版本不匹配，请重新核对')
