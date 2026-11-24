"""Charge local creation peaks under the caller's owner quota transaction.

No independent SQL commit: the drained filesystem thread cannot outlive this
lock scope. A crashed writer leaves files for the physical usage scanner.
"""
import asyncio,threading
from contextlib import asynccontextmanager,contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .. import storage
from ..models import User
from .storage_admission import lock_owner

_current=ContextVar('local_provision_capacity',default=None)
_node=ContextVar('local_provision_node_capacity',default=None)


@dataclass
class NodeScope:
    user_id:int
    maximum:int
    owner_peak_admitted:bool=False
    retained:int=0
    closed:bool=False


@asynccontextmanager
async def node_batch(user_id, maximum, *, reserve_owner=False):
    """Hold a node claim, with an optional up-front owner check for a case scaffold."""
    from .node_storage_budget import allocation
    if _node.get() is not None:raise ValueError('Local node creation cannot be nested')
    if type(maximum) is not int or maximum<0:raise ValueError('Invalid creation node bound')
    owner_peak_admitted = False
    if reserve_owner:
        owner = _current.get()
        if owner is None or owner.user.id != user_id:
            raise ValueError('Local node creation requires its owner transaction')
        owner.check_transaction()
        from .quota import fits_locked_storage_quota
        owner_peak_admitted = await fits_locked_storage_quota(owner.db, owner.user, maximum)
    async with allocation(maximum):
        scope=NodeScope(user_id,maximum,owner_peak_admitted=owner_peak_admitted);scope.lock=threading.Lock()
        token=_node.set(scope)
        try:yield
        finally:scope.closed=True;_node.reset(token)


@contextmanager
def node_write(prefix,peak,retained):
    from .node_storage_budget import reserve
    scope=_node.get()
    if scope is None:
        with reserve(peak):yield
        return
    if (scope.closed or not prefix.startswith(f'workspaces/{scope.user_id}/')
            or type(peak) is not int or type(retained) is not int or not 0<=retained<=peak):
        raise ValueError('Local node write differs from its creation scope')
    if not scope.lock.acquire(blocking=False):raise ValueError('Local creation writes must drain serially')
    try:
        if scope.retained+peak>scope.maximum:raise ValueError('Local creation exceeded its reserved node peak')
        try:yield
        finally:
            # Even a failed no-replace write is charged conservatively. Its
            # temporary file and intent are drained before this context exits.
            scope.retained+=retained
    finally:scope.lock.release()


@dataclass
class Scope:
    db: object
    user: object
    transaction: object
    loop: object
    closed: bool=False

    def check_transaction(self):
        if self.closed or self.db.get_transaction() is not self.transaction or not self.transaction.is_active:
            raise ValueError('Local creation capacity lost its caller transaction')

    async def admit(self,size,*,check_bytes=True):
        self.check_transaction()
        if check_bytes:
            from .quota import check_storage_quota
            await check_storage_quota(self.db,self.user,size)


def admit(prefix,size):
    scope=_current.get()
    if scope is None or scope.closed:raise ValueError('Local creation requires its owner capacity context')
    if not prefix.startswith(f'workspaces/{scope.user.id}/'):
        raise ValueError('Local creation crosses its quota owner')
    try:running=asyncio.get_running_loop()
    except RuntimeError:running=None
    if running is scope.loop:raise ValueError('Local creation I/O must run outside the event loop')
    batch = _node.get()
    reserved = batch is not None and batch.owner_peak_admitted
    if reserved:
        if batch.closed or batch.user_id != scope.user.id:
            raise ValueError('Local creation owner allocation is no longer active')
    # node_write charges every physical peak against the whole batch. Verify
    # the transaction on its owning loop even when the byte check is reserved.
    asyncio.run_coroutine_threadsafe(scope.admit(size,check_bytes=not reserved),scope.loop).result()


@asynccontextmanager
async def hold(db,user_id,*,wait=False):
    if storage._use_s3():
        yield
        return
    if _current.get() is not None:raise ValueError('Local creation capacity cannot be nested')
    # A try-lock on PostgreSQL avoids waiting behind an inverted case lock.
    # SQLite uses the same writer transaction already held by the caller.
    await lock_owner(db,user_id,wait=wait)
    user=await db.get(User,user_id)
    if user is None:raise ValueError('Local creation owner disappeared')
    scope=Scope(db,user,db.get_transaction(),asyncio.get_running_loop())
    token=_current.set(scope)
    try:yield scope
    finally:scope.closed=True;_current.reset(token)
