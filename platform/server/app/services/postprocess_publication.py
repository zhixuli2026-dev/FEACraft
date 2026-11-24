"""Recompute extraction evidence and gate the canonical postprocessing version."""
import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging

from .. import storage
from . import attempt_workspace as workspace, postprocess_executor as executor
from .case_evidence import file_record, is_run_input, run_inventory, safe_run_dir, verify_records
from .postprocess_artifacts import (LATEST, TOOLCHAIN, config_path, result_dir, context,
    configuration, derived_config, managed, reference_paths, snapshot)

logger = logging.getLogger(__name__)


def source_snapshot(prefix,run_dir,*,case_inputs=False):
    from .case_evidence import safe_run_dir
    run_dir=safe_run_dir(run_dir)
    return inspect_inputs(prefix,run_dir,
        lambda root:_source_snapshot(root,run_dir,case_inputs=case_inputs),case_inputs=case_inputs)


def inspect_inputs(prefix,run_dir,reader,*,case_inputs=False,include_run_inputs=False,include_postprocess_targets=False):
    from .case_read_view import inspect
    from .case_evidence import safe_run_dir
    from .casedict import parse_casedict
    run_dir=safe_run_dir(run_dir)
    # Registered extraction takes fields from immutable solve references; its
    # live source consists of configuration and external reference files. The
    # case-file variant additionally owns the current run inputs and results.
    # Discover candidates first, then read/recheck caseDict inside the view.
    # A concurrent reference-path change cannot silently select absent bytes:
    # _source_snapshot reparses it and inventories every reference again.
    from .local_source_reads import observe
    with observe(storage._fs_path(prefix,'.'),['0-caseDict/caseDict']):
        config=executor.config_from_case_dict(parse_casedict(
            storage.get_bytes(prefix,'0-caseDict/caseDict').decode('utf-8')),run_dir)
    paths=['0-caseDict/caseDict',*reference_paths(config),*([run_dir] if case_inputs else [])]
    optional=[]
    if include_run_inputs and not case_inputs:
        paths.append(run_dir+'/system')
        optional=[run_dir+'/'+name for name in ('constant','0','0.orig')]
    if include_postprocess_targets:
        optional.append('D-post')
    return inspect(prefix,reader,paths=paths,optional_paths=optional)


def _source_snapshot(root,run_dir,*,case_inputs=False):
    config=configuration(root,run_dir,case_inputs=case_inputs)
    workspace.inventory(root,['0-caseDict/caseDict',*reference_paths(config),*([run_dir] if case_inputs else [])])
    return snapshot(root,run_dir,case_inputs=case_inputs)


def artifacts_for(run,descriptor,output,frozen_files):
    identity=context(run)
    case_inputs=descriptor.get('handler')=='postprocess-case.v1'
    case_id,job_id=(identity['source'][key] for key in ('case_id','job_id'))
    if case_id!=descriptor['case_id'] or snapshot(run,descriptor['run_dir'],case_inputs=case_inputs)!=descriptor['source']:
        raise ValueError('Postprocessing source differs from preparation')
    for path,record in frozen_files.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(actual[key]!=record[key] for key in ('size','sha256')):
            raise ValueError('Postprocessing modified its versioned input: '+path)
    cfg=derived_config(run,descriptor['run_dir'],descriptor['execution_id'],case_inputs=case_inputs)
    config_relative=config_path(job_id)
    if (run/config_relative).read_bytes()!=executor._json_bytes(cfg):
        raise ValueError('Postprocessing config differs from its sealed caseDict')
    directory=result_dir(job_id)
    metrics=json.loads((run/directory/'metrics.json').read_text(encoding='utf-8'))
    if datetime.fromisoformat(metrics['generated_at']).tzinfo is None:
        raise ValueError('Postprocessing timestamp has no timezone')
    recomputed,artifacts=executor.evaluate(run,case_id,job_id,cfg,generated_at=metrics['generated_at'])
    if metrics!=json.loads(executor._json_bytes(recomputed)):
        raise ValueError('Postprocessing metrics/comparisons/readiness differ from actual fields')
    for name,data in artifacts.items():
        if (run/directory/name).read_bytes()!=data:
            raise ValueError('Postprocessing output differs from recomputation: '+name)
    manifest=json.loads((run/directory/'artifact-manifest.json').read_text(encoding='utf-8'))
    run_paths=run_inventory(run,descriptor['run_dir'])
    from .postprocess_reference_inputs import bound_metrics_paths
    inputs=set(run_paths)|{run/config_relative}|{run/path for path in reference_paths(cfg)+bound_metrics_paths(cfg)}
    expected={'schema':'vibeflow.postprocess.artifacts.v1','case_id':case_id,'job_id':job_id,
        'run_dir':descriptor['run_dir'],'config_path':config_relative,
        'config_sha256':hashlib.sha256((run/config_relative).read_bytes()).hexdigest(),
        'source':cfg['_source'],'readiness':recomputed['readiness'],
        'inputs':[file_record(run,path) for path in sorted(inputs)],
        'run_inventory':[path.relative_to(run).as_posix() for path in run_paths],
        'artifacts':[{'path':directory+'/'+name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            for name,data in sorted(artifacts.items())]}
    if any(manifest.get(key)!=value for key,value in expected.items()):
        raise ValueError('Postprocessing manifest differs from actual inputs/outputs')
    latest={'job_id':job_id,'result_dir':directory,'metrics_path':directory+'/metrics.json',
        'manifest_path':directory+'/artifact-manifest.json'}
    if json.loads((run/LATEST).read_text(encoding='utf-8'))!=latest:
        raise ValueError('Postprocessing pointer belongs to another result')
    toolchain=json.loads((run/TOOLCHAIN).read_text(encoding='utf-8'))
    expected_toolchain={'schema':'vibeflow.postprocess-toolchain.v1','case_id':case_id,'job_id':job_id,
        'attempt_id':identity['attempt_id'],'execution_id':descriptor['execution_id'],
        'worker_sha256':descriptor['worker_sha256'],'executor_sha256':descriptor['executor_sha256']}
    if (any(toolchain.get(key)!=value for key,value in expected_toolchain.items()) or
            any(not toolchain.get(key) for key in ('python','vtk','numpy','matplotlib')) or
            toolchain['vtk']!=manifest.get('vtk_version')):
        raise ValueError('Postprocessing toolchain does not belong to this invocation')
    if output.get('schema')!='vibeflow.attempt-outputs.v1':
        raise ValueError('Postprocessing output inventory is missing')
    required={LATEST,TOOLCHAIN,config_relative,directory+'/artifact-manifest.json'}|{directory+'/'+name for name in artifacts}
    records={path:record for path,record in output['files'].items() if managed(path,job_id)}
    if set(records)!=required:
        raise ValueError('Postprocessing publication is incomplete or contains unregistered outputs')
    for path,record in records.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(actual[key]!=record[key] for key in ('size','sha256')):
            raise ValueError('Postprocessing output changed after capture')
    return records


async def gate_reason(db,case,*,freshness=None):
    from .artifact_publication import current
    from .case_write import CaseWriteConflict
    from .publication_inputs import resolve
    from .async_ops import finish_before_cancel
    from .file_read import FileReadConflict
    try:
        head,row=await current(db,case.id,'postprocess')
        if head is None:
            return None,False
        if row is None:
            return '当前后处理成果尚未发布或已被修改，请重新提取',True
        resolved=await resolve(db,case,[{'publication_id':row.id,'paths':None,'require_current':True}],
                               materialize=False,_validated_current=freshness)
        return await finish_before_cancel(asyncio.to_thread(_gate_files,case.s3_prefix,row,resolved[0].files)),True
    except TimeoutError as error:
        raise CaseWriteConflict('validation_busy','后处理证据核验超时，请稍后重试') from error
    except (ValueError,OSError,KeyError,TypeError,FileReadConflict) as error:
        logger.warning('Postprocess confirmation evidence read failed (case=%s type=%s)',case.id,type(error).__name__)
        return '后处理发布或上游输入已失效，请重新提取',True


def _gate_files(prefix,row,files):
    from .file_read import ArchivedPublication
    from .publication_result_reads import archive_json
    reason=verify_records(prefix,[{'path':path,**record} for path,record in files.items()],
        label='当前后处理',required=True,parallel_remote=True)
    if reason:
        return reason
    with ArchivedPublication(prefix,row) as archive:
        pointer=archive_json(archive,LATEST)
        metrics=archive_json(archive,pointer['metrics_path'])
        manifest=archive_json(archive,pointer['manifest_path'])
    reason=verify_records(prefix,manifest['inputs'],label='后处理输入与参考',required=True,parallel_remote=True)
    if reason:
        return reason
    observed = _gate_inventory(prefix, manifest['run_dir'])
    if observed!=manifest['run_inventory']:
        return '当前求解输入或输出时刻目录已变化，请重新提取'
    if metrics['readiness'].get('report_ready') is not True:
        return '后处理 readiness 未满足: '+'；'.join(map(str,metrics['readiness'].get('blockers',[])))
    return None


def _gate_inventory(prefix, run_dir):
    """Observe field paths without constructing an S3 file view."""
    run_dir = safe_run_dir(run_dir)
    if not storage._use_s3():
        root = storage._fs_path(prefix, '.')
        return [path.relative_to(root).as_posix() for path in run_inventory(root, run_dir)]

    from .attempt_objects import selection
    from .case_write import CaseWriteConflict
    versions = selection(prefix, [run_dir], False)
    observed = sorted(path for path in versions if is_run_input(path, run_dir))
    if selection(prefix, [run_dir], False) != versions:
        raise CaseWriteConflict('input_stale', 'S3 case changed during evidence inventory')
    return observed


@dataclass(frozen=True)
class LegacyResult:
    prefix: str
    revision: int
    sequence: int | None
    job_id: int | None


async def select_result(db, case, publication_id, job_id):
    from fastapi import HTTPException
    from sqlalchemy import select
    from ..models import ArtifactPublication, Job, JobAttempt
    from .artifact_publication import current
    from .publication_result_reads import Selection, case_version

    if publication_id and job_id is not None:
        raise HTTPException(400, '请只指定 publication_id 或 job_id')
    if job_id is not None and job_id <= 0:
        raise HTTPException(400, 'job_id 必须为正整数')

    head, active = await current(db, case.id, 'postprocess')
    if publication_id:
        row = await db.get(ArtifactPublication, publication_id, populate_existing=True)
    elif job_id is not None:
        row = await db.scalar(select(ArtifactPublication).join(JobAttempt,
            ArtifactPublication.attempt_id == JobAttempt.id).where(JobAttempt.job_id == job_id,
            ArtifactPublication.case_id == case.id, ArtifactPublication.kind == 'postprocess'))
    else:
        row = active
    if row is None and not publication_id and (job_id is not None or head is None):
        revision, sequence = await case_version(db, case)
        return LegacyResult(case.s3_prefix, revision, sequence, job_id)
    if row is None or row.case_id != case.id or row.kind != 'postprocess':
        raise HTTPException(404, '后处理发布不存在')
    job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
                          .where(JobAttempt.id == row.attempt_id))
    if (row.status not in {'committed', 'superseded'} or job is None
            or job.case_id != case.id or job.status != 'succeeded'):
        raise ValueError('Postprocessing publication has no successful owned Job')
    return await Selection.capture(db, case, row, active, job.id)


def read_result_body(selection):
    from .file_read import ArchivedPublication
    from .publication_inputs import _selected_members
    from .publication_result_reads import archive_json

    if isinstance(selection, LegacyResult):
        return _legacy_result(selection)
    with ArchivedPublication(selection.prefix, selection.publication) as archive:
        _selected_members(archive.manifest, None)
        pointer = archive_json(archive, LATEST)
        metrics_path, manifest_path = pointer['metrics_path'], pointer['manifest_path']
        metrics = archive_json(archive, metrics_path)
        manifest = archive_json(archive, manifest_path)
        toolchain = archive_json(archive, TOOLCHAIN)
        archive.verify_other_than({LATEST, metrics_path, manifest_path, TOOLCHAIN})
    return {'job_id': pointer['job_id'], 'result_dir': pointer['result_dir'], 'metrics': metrics,
            'manifest': manifest, 'toolchain': toolchain, 'publication': selection.publication_fields()}


def _legacy_result(selection):
    from fastapi import HTTPException
    from .case_write import CaseWriteConflict
    from .file_read import FileRead, FileReadConflict

    prefix, job_id = selection.prefix, selection.job_id
    files = {}

    def document(path):
        reader = files[path] = FileRead(prefix, path)
        with closing(reader.chunks()) as blocks:
            return json.loads(b''.join(blocks))

    try:
        if job_id is None:
            try:
                job_id = int(document(LATEST)['job_id'])
            except FileNotFoundError as error:
                raise HTTPException(404, '尚无成功的后处理结果') from error
            except (ValueError, TypeError, KeyError) as error:
                raise HTTPException(500, '最近后处理结果指针损坏') from error
        if job_id <= 0:
            raise HTTPException(400, 'job_id 必须为正整数')
        directory = result_dir(job_id)
        try:
            metrics = document(directory + '/metrics.json')
            manifest = document(directory + '/artifact-manifest.json')
        except FileNotFoundError as error:
            raise HTTPException(404, f'作业 #{job_id} 没有完整后处理结果') from error
        except (json.JSONDecodeError, UnicodeError) as error:
            raise HTTPException(500, '后处理结果 JSON 损坏') from error
        for path, original in files.items():
            try:
                if FileRead(prefix, path).version != original.version:
                    raise FileReadConflict('Legacy postprocessing result changed')
            except FileNotFoundError as error:
                raise FileReadConflict('Legacy postprocessing result was removed') from error
    except FileReadConflict as error:
        raise CaseWriteConflict('input_stale', '后处理结果在读取期间发生变化，请重试') from error

    return {'job_id': job_id, 'result_dir': directory, 'metrics': metrics, 'manifest': manifest}
