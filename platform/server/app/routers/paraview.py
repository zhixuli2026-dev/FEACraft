"""ParaView 执行入口和 Server Manager 目录。"""

import asyncio
import json
import subprocess
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..services.case_write_capacity import committing
from ..db import get_db
from ..deps import get_current_user
from ..models import User,JobAttempt
from ..services import legacy_job_admission as admission
from ..services import paraview_catalog, paraview_pipeline
from ..services.paraview import catalyst_versions, paraview_executable
from ..storage import exists, file_size
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1/cases/{case_id}/paraview", tags=["paraview"])


class ParaViewIn(BaseModel):
    script_path: str
    backend: str = "pvpython"


class CatalystIn(BaseModel):
    driver_path: str
    state_path: str
    version: Literal[1, 2] = 2
    backend: Literal['pvpython', 'pvbatch'] = 'pvpython'


class PipelineIn(BaseModel):
    nodes: list[dict[str, JsonValue]] = Field(min_length=1, max_length=32)
    backend: Literal['pvpython', 'pvbatch'] = 'pvpython'
    animation: dict[str, JsonValue] | None = None
    extractors: list[dict[str, JsonValue]] | None = Field(default=None, max_length=16)
    inspection: dict[str, JsonValue] | None = None
    plugins: list[str] | None = Field(default=None,max_length=8)
    volume_format: Literal['NIfTI','Analyze'] | None = None


async def _catalogue(case_id, user, db):
    await get_owned_case(case_id, user, db, write=False)
    await db.rollback()
    try:
        return await asyncio.to_thread(paraview_catalog.catalogue)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise HTTPException(503, '已配置的 ParaView 无法提供目录') from error


@router.get('/catalog')
async def list_catalog(case_id: int, group: Literal['sources','filters'] | None = None,
                       q: str = Query('',max_length=160), user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    catalogue = await _catalogue(case_id,user,db)
    return {'schema':catalogue['schema'],'version':catalogue['version'],
            'items':paraview_catalog.search(catalogue,group,q),
            'pipeline_operators': {group: sorted(names & catalogue['groups'][group].keys())
                                   for group, names in paraview_pipeline.OPERATORS.items()}}


@router.get('/catalog/{group}/{name}')
async def catalog_proxy(case_id: int, group: Literal['sources','filters'], name: str,
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    catalogue = await _catalogue(case_id,user,db)
    entry = catalogue['groups'][group].get(name)
    if entry is None:
        raise HTTPException(404,'ParaView 目录中没有此项')
    return entry


@router.get('/catalog/inputs')
async def catalog_inputs(case_id: int, path: str = Query(...,max_length=1000),
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from functools import partial
    from ..services.case_read_view import inspect
    from ..services.case_write import CaseWriteConflict, bounded_case_read

    case = await get_owned_case(case_id,user,db,write=False)
    prefix = case.s3_prefix
    await db.rollback()
    try:
        relative = paraview_pipeline.dataset_path(path)
        read = partial(inspect, prefix, paraview_catalog.matching, relative,
                       paths=[relative], max_file_bytes=paraview_pipeline.MAX_BYTES)
        return await bounded_case_read(read, code='paraview_catalog_input', label='ParaView 输入域')
    except FileNotFoundError as error:
        raise HTTPException(404, 'ParaView 程序或源文件不可用') from error
    except CaseWriteConflict as error:
        raise HTTPException(409, {'code':error.code, 'message':str(error)}) from error
    except (ValueError,subprocess.SubprocessError) as error:
        raise HTTPException(422, 'ParaView 无法核验所选数据集的输入域') from error


@router.get('/plugins')
async def list_plugins(case_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    catalogue = await _catalogue(case_id,user,db)
    return {'version':catalogue['version'],'plugins':list(catalogue['plugins'].values())}


@router.get('/plugins/{name}')
async def get_plugin(case_id: int, name: str, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db)):
    catalogue = await _catalogue(case_id,user,db)
    if name not in catalogue['plugins']:
        raise HTTPException(404,'ParaView 当前运行环境未登记此插件')
    try:
        result = await asyncio.to_thread(paraview_catalog.catalogue,name)
        formats = paraview_pipeline.PLUGIN_READERS.get(name,{})
        render_modes = [{'name':mode,'components':definition[2]}
                        for mode, definition in paraview_pipeline.PLUGIN_REPRESENTATIONS.items() if definition[0]==name]
        if name == 'PanoramicProjectionView':
            render_modes = [{'name':mode,'projection':True} for mode in paraview_pipeline.PANORAMIC_VIEWS]
        return {**result,'pipeline_operators':sorted(row['name'] for row in result['proxies'] if
                                                     row['name'] in paraview_pipeline.PLUGIN_OPERATORS.get(name,())),
                'reader_formats':{row['name']:formats[row['name']] for row in result['proxies'] if row['name'] in formats},
                'render_modes':render_modes}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise HTTPException(503,'当前 ParaView 无法加载此插件') from error


@router.get('/dataset')
async def pipeline_dataset(case_id: int, path: str = Query(...,max_length=1000),
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from functools import partial
    from ..services.case_read_view import inspect
    from ..services.case_write import CaseWriteConflict, bounded_case_read

    case = await get_owned_case(case_id,user,db,write=False)
    prefix = case.s3_prefix
    await db.rollback()
    try:
        relative = paraview_pipeline.dataset_path(path)
        read = partial(inspect, prefix, paraview_pipeline.dataset_information, relative,
                       paths=[relative], max_file_bytes=paraview_pipeline.MAX_BYTES)
        return await bounded_case_read(read, code='paraview_dataset', label='ParaView 数据集读取')
    except FileNotFoundError as error:
        raise HTTPException(404, 'ParaView 程序或源文件不可用') from error
    except CaseWriteConflict as error:
        raise HTTPException(409, {'code':error.code, 'message':str(error)}) from error
    except subprocess.SubprocessError as error:
        raise HTTPException(503, 'ParaView 数据集读取超时') from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post(":run")
async def run_paraview(case_id: int, body: ParaViewIn,
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                       idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_run(db,case,user,body,idempotency_key)


@router.post('/pipeline')
async def run_pipeline(case_id: int, body: PipelineIn,
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                       idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_run(db,case,user,body,idempotency_key)


@router.post('/catalyst')
async def run_catalyst(case_id: int, body: CatalystIn,
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                       idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    case = await get_owned_case(case_id, user, db)
    return await _registered_run(db,case,user,body,idempotency_key)


@router.get('/catalyst/capabilities')
async def catalyst_capabilities(case_id: int, user: User = Depends(get_current_user),
                                db: AsyncSession = Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    backends = {}
    for backend in ('pvpython','pvbatch'):
        try:
            backends[backend] = catalyst_versions(paraview_executable(backend))
        except FileNotFoundError:
            continue
    return {'backends':backends}


async def _registered_run(db,case,user,body,key):
    from ..services import job_execution,step_handlers
    from ..services.case_write import CaseWriteConflict
    from ..services.workflow_state import ExecutionConflict
    from ..services.paraview_artifacts import script_path
    try:
        job_execution.validate_key(key)
        pipeline = isinstance(body, PipelineIn)
        catalyst = isinstance(body, CatalystIn)
        endpoint = 'paraview.catalyst' if catalyst else 'paraview.pipeline' if pipeline else 'paraview.run'
        payload = body.model_dump()
        if pipeline and body.animation is None:
            payload.pop('animation')
        if pipeline and body.extractors is None:
            payload.pop('extractors')
        if pipeline and body.inspection is None:
            payload.pop('inspection')
        if pipeline and body.plugins is None:
            payload.pop('plugins')
        if pipeline and body.volume_format is None:
            payload.pop('volume_format')
        digest=job_execution.request_hash({'endpoint':endpoint,'payload':payload})
        output = {}
        async with committing(db,case):
            job=await job_execution.lookup_submission(db,case.id,user.id,key,digest)
            replay=job is not None
            if not replay:
                if pipeline:
                    parameters = {'nodes': body.nodes}
                    if body.animation is not None:
                        parameters['animation'] = body.animation
                    if body.extractors is not None:
                        parameters['extractors'] = body.extractors
                    if body.inspection is not None:
                        parameters['inspection'] = body.inspection
                    if body.plugins is not None:
                        parameters['plugins'] = body.plugins
                    if body.volume_format is not None:
                        parameters['volume_format'] = body.volume_format
                elif catalyst:
                    parameters = {'script_path': script_path(body.driver_path),
                                  'catalyst': {'state_path':script_path(body.state_path),'version':body.version}}
                else:
                    path=script_path(body.script_path)
                    if not exists(case.s3_prefix,path):raise HTTPException(404,'ParaView script does not exist')
                    if file_size(case.s3_prefix,path)>1024*1024:raise HTTPException(413,'ParaView script exceeds 1 MiB')
                    parameters = {'script_path': path}
                try:executable=paraview_executable(body.backend)
                except FileNotFoundError as error:raise HTTPException(503,str(error)) from error
                prepared=await step_handlers.get_handler('paraview.run').prepare(db,case,user,
                    parameters={**parameters,'backend':body.backend,'executable':executable},
                    budget={'timeout_seconds':86400,'max_log_bytes':16*1024*1024})
                job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=key,digest=digest,
                    job_type=prepared.job_type,command=prepared.spec['command'],spec=prepared.spec,
                    input_paths=prepared.input_paths,generated_inputs=prepared.generated_inputs)
                attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
                await step_handlers.bind(db,case,job,attempt,prepared)
            if pipeline or catalyst:
                attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id)
                                          .order_by(JobAttempt.number.desc()).limit(1))
                descriptor = json.loads(attempt.publication_json)['descriptor']
                if pipeline:
                    folder = descriptor['configuration']['pipeline']['folder']
                    output = {'output_dir': folder, 'report_path': folder+'/pipeline.json'}
                else:
                    folder = descriptor['configuration']['catalyst']['folder']
                    output = {'output_dir': folder, 'report_path': folder+'/catalyst.json'}
        if not replay:
            from ..services.job_delivery import deliver,DeliveryDeferred
            try:await deliver(job.id)
            except DeliveryDeferred:pass
        return await admission.response(db,job,backend=body.backend,**output)
    except ExecutionConflict as error:raise HTTPException(409,{'code':error.code,'message':error.detail}) from error
    except FileNotFoundError as error:raise HTTPException(404,'ParaView input does not exist: '+str(error)) from error
    except subprocess.SubprocessError as error:raise HTTPException(503,'ParaView plugin probe timed out') from error
    except ValueError as error:
        if isinstance(error,CaseWriteConflict):raise
        raise HTTPException(400,str(error)) from error
