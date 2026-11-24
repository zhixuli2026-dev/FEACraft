"""Observe actual CLI owner's retained bytes without holding SQL during scans.

This is periodic enforcement, not an OS quota or write reservation. Admission
continues to account for outstanding reservations under its own owner lock.
"""
import asyncio
from ..config import get_settings
from ..db import SessionLocal
from ..models import User
from .case_write import CaseWriteConflict
from .quota import quotas_for,_storage_limit
from .storage_usage import measure_user


async def observe(user_id):
    try:
        # The caller selects the startup or runtime scope. The inventory
        # drains its filesystem worker before cancellation returns.
        async with asyncio.timeout(get_settings().storage_measurement_timeout_seconds):
            usage=await measure_user(user_id)
            async with SessionLocal() as db:
                user=await db.get(User,user_id)
                if user is None or user.status!='active':
                    raise CaseWriteConflict('cli_user_unavailable','CLI 用户已不可用')
                limit=_storage_limit(await quotas_for(db,user))
            if not usage.complete:
                raise ValueError('Incomplete CLI owner inventory')
            if usage.bytes>limit:
                raise CaseWriteConflict('cli_user_storage_full','CLI 用户的实际存储用量已超过套餐额度')
            return {'user_id':user_id,'bytes':usage.bytes,'limit_bytes':limit,'entries':usage.entries}
    except asyncio.CancelledError:raise
    except CaseWriteConflict:raise
    except Exception as error:
        raise CaseWriteConflict('cli_user_storage_unavailable','CLI 用户存储用量暂时无法核验') from error
