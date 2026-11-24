"""Wait for another owner's writer to settle before immutable-copy admission.

Call only outside case/business transactions. Each operation must open and close
its own reservation transaction before returning or raising. This repeats quota
admission, never native execution or an uncertain remote write.
"""
import asyncio
import time

from fastapi import HTTPException
from . import execution_budget
from .case_write import CaseWriteConflict


async def admit(operation,*,case_contention=False):
    budget=execution_budget.current()
    deadline=budget.deadline if budget is not None else time.monotonic()+30
    while True:
        if budget is not None:budget.check()
        try:return await operation()
        except (HTTPException,CaseWriteConflict) as error:
            busy=(isinstance(error,HTTPException) and error.status_code==409 and isinstance(error.detail,dict)
                and error.detail.get('code')=='storage_quota_busy')
            if case_contention and isinstance(error,CaseWriteConflict) and error.code=='validation_busy':busy=True
            if not busy:raise
            if budget is not None:budget.check()
            remaining=deadline-time.monotonic()
            if remaining<=0:raise
            await asyncio.sleep(min(.1,remaining))
