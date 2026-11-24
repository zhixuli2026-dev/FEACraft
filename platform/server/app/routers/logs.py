"""日志解析路由（对标 VFLogService）"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services import case_read_capacity
from ..services.case_write import CaseWriteConflict, bounded_case_read
from ..services.file_read import FileRead, FileReadConflict
from ..services.logparse import LogReadLimit, LogRewriteError, parse_log_file
from ..services.request_deadline import scope
from .workspaces import get_owned_case

router = APIRouter(prefix="/api/v1", tags=["logs"])


@router.get("/cases/{case_id}/logs/parse")
async def parse_log(case_id: int, path: str = Query(...), incremental: bool = False,
                    offset: int = Query(0, ge=0), fingerprint: str | None = None,
                    user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    """解析求解日志；incremental=true 时用完整行游标与前缀指纹续读。"""
    case = await get_owned_case(case_id, user, db, write=False)
    if not incremental and (offset or fingerprint is not None):
        raise HTTPException(400, "offset/fingerprint 仅用于 incremental=true")
    try:
        async with scope(get_settings().storage_measurement_timeout_seconds):
            async with case_read_capacity.reading(db, case):
                reader = await bounded_case_read(FileRead, case.s3_prefix, path, code="log_parse", label="日志解析")
            # FileRead pins the selected version. Release the transaction and any
            # case locks before scanning the file and building chart data.
            await db.rollback()
            return await bounded_case_read(parse_log_file, reader, incremental, offset, fingerprint,
                                           code="log_parse", label="日志解析")
    except FileNotFoundError as error:
        raise HTTPException(404, f"日志不存在: {path}") from error
    except FileReadConflict as error:
        raise HTTPException(409, {"code":"file_changed_during_read", "message":"日志读取期间发生变化，请重试"}) from error
    except TimeoutError as error:
        raise HTTPException(504, {"code":"log_parse_timeout", "message":"日志解析超时，请稍后重试"}) from error
    except LogReadLimit as error:
        raise HTTPException(413, str(error)) from error
    except LogRewriteError as error:
        raise HTTPException(409, str(error)) from error
    except CaseWriteConflict:
        raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
