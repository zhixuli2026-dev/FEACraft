"""Owned composition history, application status and optional build lineage."""
import json

from fastapi import HTTPException

from . import compose_artifacts as artifacts


async def result(db,case,publication_id=None,output=None):
    from ..models import ArtifactPublication,JobAttempt
    from .artifact_publication import current,read_file,read_manifest
    from .publication_inputs import resolve
    try:
        if output is not None and output not in artifacts.OUTPUTS:
            raise HTTPException(400,'报告输出路径不受支持')
        row=await db.get(ArtifactPublication,publication_id) if publication_id else (await current(
            db,case.id,artifacts.OUTPUTS[output or 'E-report/report.md'][1]))[1]
        if row is None or row.case_id!=case.id or row.kind not in {value[1] for value in artifacts.OUTPUTS.values()}:
            raise HTTPException(404,'报告草稿版本不存在')
        _,manifest=read_manifest(case.s3_prefix,row)
        descriptor=manifest['descriptor']
        if output is not None and descriptor['output']!=output:
            raise HTTPException(404,'报告草稿版本不属于此输出')
        await resolve(db,case,[{'publication_id':row.id,'paths':None,'require_current':False}])
        _,active=await current(db,case.id,row.kind)
        is_current=active is not None and active.id==row.id
        try:
            await resolve(db,case,[{'publication_id':row.id,'paths':None,'require_current':True}])
            is_applied=is_current and artifacts.applied(case.s3_prefix,row,manifest)
            stale=not is_applied
        except (ValueError,OSError,KeyError):
            is_applied,stale=False,True
        folder=artifacts.directory(descriptor['execution_id'])
        document=json.loads(read_file(case.s3_prefix,row,folder+'/result.json'))
        attempt=await db.get(JobAttempt,row.attempt_id)
        return row,{**document,'job_id':attempt.job_id,'status':'succeeded',
            'publication_id':row.id,'generation':row.generation,'source_path':folder+'/report.md',
            'toolchain':json.loads(read_file(case.s3_prefix,row,folder+'/toolchain.json')),
            'publication':{'id':row.id,'generation':row.generation,'status':row.status,
                'current':is_current,'stale':stale,'applied':is_applied}}
    except (ValueError,OSError,KeyError,TypeError) as error:
        raise HTTPException(409,{'code':'evidence_invalid','message':'报告草稿发布记录或文件损坏'}) from error


async def build_reference(db,case,expected=None):
    """Track an applied generated draft; manually edited drafts remain user input."""
    from .artifact_publication import current,read_file,read_manifest
    from .case_write import bounded_case_read
    from .publication_inputs import resolve
    from .workflow_state import ExecutionConflict
    _,row=await current(db,case.id,'report-source')
    if row is None:
        return []
    def compare():
        _,manifest=read_manifest(case.s3_prefix,row)
        path=artifacts.directory(manifest['descriptor']['execution_id'])+'/report.md'
        live=artifacts.live_existing(case.s3_prefix,'E-report/report.md')
        if expected is not None and (live is None or artifacts.record(live)!=expected):
            raise ExecutionConflict('input_stale','Report draft changed while binding its publication')
        return path,manifest,live==read_file(case.s3_prefix,row,path)

    path,manifest,matches=await bounded_case_read(compare,code='report_preparation',label='核验报告草稿来源')
    if not matches:
        return []
    reference={'publication_id':row.id,'paths':[path],'require_current':True}
    try:
        await resolve(db,case,[reference])
        if not await bounded_case_read(artifacts.applied,case.s3_prefix,row,manifest,
                                       code='report_preparation',label='核验报告草稿发布'):
            raise ValueError('Composition was not fully applied')
    except (ValueError,OSError,KeyError) as error:
        raise ExecutionConflict('input_stale','Generated report draft is stale; compose a fresh draft before building') from error
    return [reference]
