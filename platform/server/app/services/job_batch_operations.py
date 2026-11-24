"""Registered professional members for the existing same-case Job batch API."""
from functools import partial
import json
from typing import Annotated,Literal

from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field,TypeAdapter
from sqlalchemy import select

from ..models import JobAttempt
from . import job_delivery,job_execution,registered_input_seal,step_handlers
from .case_write import CaseWriteConflict
from .mpi_handler import run_path

SCHEMA='vibeflow.job-batch.v2'


class MpiOperationIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['mpi']
    run_dir:str
    solver:str='icoFoam'
    subdomains:int=Field(default=2,ge=1,le=64)
    timeout_seconds:int=Field(default=86400,ge=1,le=86400)
    max_log_bytes:int=Field(default=16777216,ge=1024,le=67108864)


class UdfOperationIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['udf']
    run_dir:str
    name:str=Field(default='myFunctionObject',pattern=r'^[A-Za-z][A-Za-z0-9_]{0,39}$')
    timeout_seconds:int=Field(default=86400,ge=1,le=86400)
    max_log_bytes:int=Field(default=16777216,ge=1024,le=67108864)


class SerialOperationIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['serial']
    run_dir:str
    solver:str='icoFoam'
    timeout_seconds:int=Field(default=86400,ge=1,le=86400)
    max_log_bytes:int=Field(default=16777216,ge=1024,le=67108864)


OperationIn=Annotated[MpiOperationIn|UdfOperationIn|SerialOperationIn,Field(discriminator='kind')]
OPERATION=TypeAdapter(OperationIn)


def binding(operation):
    if operation.kind=='mpi':
        return 'mpi.openfoam',{k:getattr(operation,k) for k in ['run_dir','solver','subdomains']}
    if operation.kind=='serial':
        return 'solve.openfoam.prepared',{k:getattr(operation,k) for k in ['run_dir','solver']}
    return 'udf.integrate',{k:getattr(operation,k) for k in ['name','run_dir']}


def validate(operations):
    paths=[]
    for operation in operations:
        try:path=run_path(operation.run_dir)
        except ValueError as error:raise HTTPException(400,str(error)) from error
        if path!=operation.run_dir:raise HTTPException(400,'Professional batch run directories must be canonical')
        key=path.casefold()
        if any(key==p or key.startswith(p+'/') or p.startswith(key+'/') for p in paths):
            raise HTTPException(400,'Professional batch run directories must be independent')
        paths.append(key)


async def prepare(db,case,user,operation):
    try:
        handler,parameters=binding(operation)
        return await step_handlers.get_handler(handler).prepare(db,case,user,parameters=parameters,
            budget={'timeout_seconds':operation.timeout_seconds,'max_log_bytes':operation.max_log_bytes})
    except (ValueError,FileNotFoundError) as error:
        if isinstance(error,CaseWriteConflict):
            raise
        raise HTTPException(400,'Professional batch input is invalid: '+str(error)) from error


def stage_local(case, user_id, operations):
    return registered_input_seal.stage_local(case,user_id,[partial(prepare,operation=value) for value in operations],
        code='operation_batch_seal',label='封存专业批次输入')


def stage_remote(db, case, user_id, key, digest, operations):
    return registered_input_seal.stage_remote(db,case,user_id,key,digest,
        [partial(prepare,operation=value) for value in operations],code='operation_batch_seal',label='封存专业批次输入')


async def members(db,manifest,rows):
    """Never trust editable display metadata as executable membership/parameters."""
    if manifest.get('schema')!=SCHEMA:return None
    try:
        payload=manifest['request'];operations=[OPERATION.validate_python(op) for op in payload['operations']]
        if (not 1<=len(operations)<=50 or payload.get('commands') is not None or payload['type']!='shell'
                or payload['runtime']!='windows' or payload['max_concurrent']!=manifest['max_concurrent']
                or [op.model_dump() for op in operations]!=payload['operations']):raise ValueError('Invalid operation request')
        validate(operations)
        digest=job_execution.request_hash({'endpoint':'jobs.batch','payload':payload})
        if digest!=manifest['request_hash'] or len(operations)!=len(manifest['job_ids']) or len(rows)!=len(operations):
            raise ValueError('Operation membership changed')
        by_id={row.id:row for row in rows};result=[]
        for identifier,operation in zip(manifest['job_ids'],operations):
            job=by_id[identifier]
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==identifier)
                .order_by(JobAttempt.number.desc()).limit(1))
            document=json.loads(attempt.input_manifest_json);handler=step_handlers.validate_binding(job,attempt)
            intent=await job_delivery.intent(db,job,attempt)
            expected={'id':manifest['id'],'jobs':manifest['job_ids'],'max_concurrent':manifest['max_concurrent']}
            descriptor=json.loads(attempt.publication_json)['descriptor'];spec=document['execution_spec']
            expected_handler,parameters=binding(operation)
            if (job.case_id!=manifest['case_id'] or document['submission_request_hash']!=digest
                    or handler.key!=expected_handler or intent['batch']!=expected
                    or any(descriptor[k]!=value for k,value in parameters.items())
                    or spec['timeout_seconds']!=operation.timeout_seconds or spec['max_log_bytes']!=operation.max_log_bytes):
                raise ValueError('Operation differs from its durable execution binding')
            result.append({'job_id':identifier,'handler':handler.key,**operation.model_dump()})
        return result
    except (ValueError,KeyError,TypeError,AttributeError,HTTPException,job_execution.ExecutionConflict) as error:
        raise HTTPException(409,{'code':'evidence_invalid','message':'Professional batch metadata differs from its original admitted members'}) from error
