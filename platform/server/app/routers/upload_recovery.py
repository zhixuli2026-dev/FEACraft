"""Discover server uploads and explicitly select their recovery action."""
from typing import Literal
from fastapi import APIRouter,Depends,Header,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field
from botocore.exceptions import BotoCoreError,ClientError
from ..deps import get_current_user
from ..models import User
from ..deadline_routes import ManualFileRecoveryDeadlineRoute
from ..services import upload_recovery as recovery
from ..services.case_write import CaseWriteConflict
from ..services.file_read import FileReadConflict
from .manual_files import public_error as file_error
from ..services.uploads import UploadError

router=APIRouter(prefix='/api/v1/cases/{case_id}/upload-recovery',tags=['upload-recovery'],route_class=ManualFileRecoveryDeadlineRoute)
ERRORS=(CaseWriteConflict,ValueError,OSError,KeyError,TypeError,FileReadConflict,BotoCoreError,ClientError)


def public_error(error):
    if isinstance(error,UploadError):return HTTPException(error.status_code,str(error))
    return file_error(error)


class PrefixBlock(BaseModel):
    model_config=ConfigDict(extra='forbid')
    size:int=Field(ge=1,le=4*1024*1024,strict=True)
    sha256:str=Field(pattern=r'^[0-9a-f]{64}$')


class ActionIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['retain_for_review','resume_upload','cancel_upload']
    upload_id:str=Field(pattern=r'^[0-9a-f]{32}$')
    snapshot_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    note:str=Field(min_length=1,max_length=2000,pattern=r'^[^\x00]*$')
    source_blocks:list[str]|None=Field(default=None,max_length=256)
    prefix_blocks:list[PrefixBlock]|None=Field(default=None,max_length=256)


@router.get('')
async def listing(case_id:int,cursor:str=Query(default='',max_length=32),limit:int=Query(default=50,ge=1,le=100),user:User=Depends(get_current_user)):
    try:return await recovery.listing(case_id,user.id,cursor,limit)
    except ERRORS as error:raise public_error(error) from error


@router.get('/actions')
async def history(case_id:int,cursor:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=100),user:User=Depends(get_current_user)):
    try:return await recovery.history(case_id,user.id,cursor,limit)
    except ERRORS as error:raise public_error(error) from error


@router.post('/actions',status_code=202)
async def perform(case_id:int,body:ActionIn,idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),user:User=Depends(get_current_user)):
    try:return await recovery.perform(case_id,user.id,body.model_dump(),idempotency_key)
    except ERRORS as error:raise public_error(error) from error


@router.get('/{identifier}')
async def plan(case_id:int,identifier:str,user:User=Depends(get_current_user)):
    try:return await recovery.plan(case_id,user.id,identifier)
    except ERRORS as error:raise public_error(error) from error
