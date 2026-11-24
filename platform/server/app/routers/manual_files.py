"""Explicit user choices over retained versions; no launch or model inference."""
from typing import Literal
from fastapi import APIRouter,Depends,Header,HTTPException,Query,Request
from pydantic import BaseModel,ConfigDict,Field
from botocore.exceptions import BotoCoreError,ClientError
from ..deps import get_current_user
from ..models import User
from ..deadline_routes import ManualFileRecoveryDeadlineRoute
from ..services import manual_file_recovery as recovery
from ..services.case_write import CaseWriteConflict

router=APIRouter(prefix='/api/v1/cases/{case_id}/file-recovery',tags=['manual-file-recovery'],route_class=ManualFileRecoveryDeadlineRoute)


class ActionIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['retain_for_review','isolate_version','restore_file']
    source:str=Field(default='pending',max_length=64)
    snapshot_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    expected_revision:int=Field(ge=0,strict=True)
    expected_event_sequence:int=Field(ge=0,strict=True)
    note:str=Field(min_length=1,max_length=2000,pattern=r'^[^\x00]*$')
    isolation_id:str|None=Field(default=None,pattern=r'^[0-9a-f-]{36}$')
    file_path:str|None=Field(default=None,min_length=1,max_length=4096)
    target_path:str|None=Field(default=None,min_length=1,max_length=4096)
    expected_target_sha256:str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$')


class ContinueIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    confirmed:Literal[True]


def public_error(error):
    if isinstance(error,(BotoCoreError,ClientError)):
        return HTTPException(503,{'code':'storage_unavailable','message':'远端存储暂时无法核对，请保留原文件和同一次确认，稍后重试'})
    if isinstance(error,CaseWriteConflict):return HTTPException(409,error.detail())
    if isinstance(error,FileNotFoundError):return HTTPException(404,'保留版本或文件不存在，请刷新诊断')
    return HTTPException(409,{'code':'file_recovery_required','message':'保留版本无法完整核验，请保留原文件并核查；没有授权重放或覆盖'})


@router.get('')
async def plan(case_id:int,source:str=Query(default='pending',max_length=64),user:User=Depends(get_current_user)):
    try:return await recovery.plan(case_id,user.id,source)
    except (CaseWriteConflict,ValueError,OSError,BotoCoreError,ClientError) as error:raise public_error(error) from error


@router.get('/actions')
async def history(case_id:int,limit:int=Query(default=50,ge=1,le=100),user:User=Depends(get_current_user)):
    try:return await recovery.history(case_id,user.id,limit)
    except (CaseWriteConflict,ValueError,OSError,BotoCoreError,ClientError) as error:raise public_error(error) from error


@router.get('/actions-page')
async def history_page(case_id:int,limit:int=Query(default=50,ge=1,le=100),before_seq:int|None=Query(default=None,ge=1),user:User=Depends(get_current_user)):
    try:return await recovery.history_page(case_id,user.id,limit,before_seq)
    except (CaseWriteConflict,ValueError,OSError,BotoCoreError,ClientError) as error:raise public_error(error) from error


@router.post('/actions',status_code=202)
async def action(case_id:int,body:ActionIn,idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),user:User=Depends(get_current_user)):
    try:return await recovery.perform(case_id,user.id,body.model_dump(),idempotency_key)
    except (CaseWriteConflict,ValueError,OSError,BotoCoreError,ClientError) as error:raise public_error(error) from error


@router.post('/actions/{identifier}:continue',status_code=202)
async def continue_action(case_id:int,identifier:str,body:ContinueIn,user:User=Depends(get_current_user)):
    try:return await recovery.continue_action(case_id,user.id,identifier,body.request_sha256)
    except (CaseWriteConflict,ValueError,OSError,BotoCoreError,ClientError) as error:raise public_error(error) from error


@router.get('/actions/{identifier}/restore-plan')
async def restore_plan(case_id:int,identifier:str,path:str=Query(min_length=1,max_length=4096),user:User=Depends(get_current_user)):
    try:return await recovery.restore_plan(case_id,user.id,identifier,path)
    except (CaseWriteConflict,ValueError,OSError,BotoCoreError,ClientError) as error:raise public_error(error) from error


@router.get('/actions/{identifier}')
async def detail(case_id:int,identifier:str,user:User=Depends(get_current_user)):
    try:return await recovery.detail(case_id,user.id,identifier)
    except (CaseWriteConflict,ValueError,OSError,BotoCoreError,ClientError) as error:raise public_error(error) from error


@router.get('/actions/{identifier}/file')
async def read_version(case_id:int,identifier:str,request:Request,path:str=Query(min_length=1,max_length=4096),
                       range_header:str|None=Header(default=None,alias='Range'),user:User=Depends(get_current_user)):
    from .files import _raw_response
    from ..services.file_read import FileReadConflict
    try:
        reader=await recovery.file_reader(case_id,user.id,identifier,path)
        return await recovery.io(_raw_response,reader,path,range_header,request.method)
    except (CaseWriteConflict,ValueError,OSError,FileReadConflict,BotoCoreError,ClientError) as error:raise public_error(error) from error
