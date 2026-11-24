"""One local monotonic admission deadline; cancellation still drains owned work."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import asyncio
import math
import time
from threading import Event

_current=ContextVar('request_deadline',default=None)
IMAGE_CLEANUP_SECONDS=300
SANDBOX_RECOVERY_SECONDS=300
STAGING_CLEANUP_SECONDS=300
CLI_STARTUP_SECONDS=45

@dataclass(frozen=True)
class Deadline:
    deadline: float
    parent: 'Deadline | None' = None
    _cancelled: Event = field(default_factory=Event, compare=False, repr=False)

    @property
    def expired(self):
        return self._cancelled.is_set() or (self.parent is not None and self.parent.expired) or time.monotonic()>=self.deadline

    def remaining(self):return 0 if self.expired else max(0,self.deadline-time.monotonic())

    def cancel(self):self._cancelled.set()

    def check(self):
        if self.expired:raise TimeoutError('Request exhausted its shared deadline')

def current():return _current.get()

@asynccontextmanager
async def scope(seconds, *, inherit=True):
    if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<=0:
        raise ValueError('Request deadline requires a finite positive duration')
    parent=current() if inherit else None
    end=time.monotonic()+seconds
    selected=Deadline(min(parent.deadline,end) if parent is not None else end,parent)
    token=_current.set(selected)
    try:
        selected.check()
        async with asyncio.timeout_at(selected.deadline):yield selected
    finally:_current.reset(token)
