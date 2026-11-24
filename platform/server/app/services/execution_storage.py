"""Bounded sampling of an attempt's files through native execution/finalizing.

This detects retained-byte growth, not cumulative bytes written or an OS quota.
Fast writers can overshoot between observations; unverified scans stop execution.
"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
from weakref import WeakKeyDictionary

from .. import storage
from ..config import get_settings
from . import request_deadline
from .async_ops import finish_before_cancel
from .storage_usage import Counter,_ordinary,_scan_fs_tree

SCHEMA='vibeflow.execution-storage-policy.v1'
_slots=WeakKeyDictionary()


def validate(policy):
    if (not isinstance(policy,dict) or set(policy)!={'schema','max_workspace_bytes','min_free_bytes'}
            or policy['schema']!=SCHEMA or type(policy['max_workspace_bytes']) is not int
            or policy['max_workspace_bytes']<1024*1024 or type(policy['min_free_bytes']) is not int
            or policy['min_free_bytes']<0):raise ValueError('Invalid frozen execution storage policy')
    return dict(policy)


def policy(previous=None):
    old=json.loads(previous.input_manifest_json).get('storage_policy') if previous else None
    if old is not None:return validate(old)
    settings=get_settings()
    return {'schema':SCHEMA,'max_workspace_bytes':settings.execution_workspace_limit_mb*1024*1024,
        'min_free_bytes':settings.node_storage_min_free_mb*1024*1024}


def limit_message(observation):
    if not observation:
        return '执行存储已达上限'
    policy = observation['policy']
    peak = observation['peak_bytes']
    free = observation['minimum_free_bytes']
    reasons = []
    if peak is not None and peak > policy['max_workspace_bytes']:
        reasons.append(f"执行目录用量 {peak} 字节超过上限 {policy['max_workspace_bytes']} 字节")
    if free is not None and free < policy['min_free_bytes']:
        reasons.append(f"磁盘可用空间 {free} 字节低于预留阈值 {policy['min_free_bytes']} 字节")
    return '，'.join(reasons) or '执行存储已达上限'


class Watch:
    def __init__(self,budget,root,*,allow_missing=False,scope='execution'):
        self.budget=budget;self.root=Path(root)
        self.base=storage._fs_root().resolve()
        self.relative=self.root.relative_to(self.base).as_posix()
        self.identity=None;self.allow_missing=allow_missing
        self.lock=asyncio.Lock()
        self.observed={'schema':'vibeflow.execution-storage-usage.v1','policy':budget.storage_policy,
            'root':self.relative,'scope':scope,'root_present':None,'samples':0,'current_bytes':None,
            'peak_bytes':None,'minimum_free_bytes':None,'state':'unobserved'}
        budget.storage_observation=self.observed

    def _snapshot(self, deadline):
        from .file_transactions import _path
        counter=Counter(get_settings(),deadline,None)
        counter.check()
        _path(self.base,self.relative,check=counter.check)
        counter.check()
        try:info=_ordinary(self.root)
        except FileNotFoundError:
            if not self.allow_missing or self.identity is not None:
                raise
            counter.check()
            free=shutil.disk_usage(self.base).free
            counter.check()
            return 0,free,False
        if self.identity is None:self.identity=(info.st_dev,info.st_ino)
        for retry in range(3):
            counter.check()
            info=_ordinary(self.root)
            if (info.st_dev,info.st_ino)!=self.identity:raise ValueError('Execution storage root changed')
            counter.bytes=0
            try:
                _scan_fs_tree(SimpleNamespace(storage_fs_root=str(self.root.parent)),(self.root.name,),counter)
                break
            except FileNotFoundError:
                # Native tools remove temporary files while running. Retry the
                # entire sample, retaining its shared time/entry bounds.
                if retry==2:raise
                counter.check()
        counter.check()
        info=_ordinary(self.root)
        if (info.st_dev,info.st_ino)!=self.identity:raise ValueError('Execution storage root changed')
        counter.check()
        free=shutil.disk_usage(self.root).free
        counter.check()
        return counter.bytes,free,True

    async def sample(self):
        if self.budget.committed:
            return
        started=time.monotonic()
        if started>=self.budget.deadline:
            self.budget.trip('timeout','deadline')
            return
        settings=get_settings()
        deadline=min(self.budget.deadline,started+settings.storage_measurement_timeout_seconds)
        parent=request_deadline.current()
        if parent is not None:
            deadline=min(deadline,parent.deadline)
        timeout=asyncio.timeout_at(deadline)
        acquired=False
        try:
            async with timeout:
                await self.lock.acquire()
                acquired=True
                if self.budget.committed:
                    return
                loop=asyncio.get_running_loop()
                slots=_slots.setdefault(loop,asyncio.Semaphore(settings.storage_measurement_concurrency))
                async with slots:
                    if self.budget.committed:
                        return
                    if time.monotonic()>=deadline:
                        raise TimeoutError('Execution storage measurement admission timed out')
                    # A filesystem call can finish after cancellation. Keep both
                    # the attempt lock and the slot until the worker drains.
                    size,free,present=await finish_before_cancel(asyncio.to_thread(self._snapshot,deadline))
                    if time.monotonic()>=deadline:
                        raise TimeoutError('Execution storage measurement timed out')
            if self.budget.committed:
                return
            previous=self.observed['peak_bytes'];previous_free=self.observed['minimum_free_bytes']
            self.observed.update(samples=self.observed['samples']+1,current_bytes=size,
                peak_bytes=max(previous or 0,size),minimum_free_bytes=min(previous_free,free) if previous_free is not None else free,
                state='measured' if present else 'absent',root_present=present)
            policy=self.budget.storage_policy
            if size>policy['max_workspace_bytes'] or free<policy['min_free_bytes']:
                self.budget.trip('disk_limit','storage')
        except (OSError,ValueError) as error:
            if self.budget.committed:
                return
            if acquired:
                self.observed.update(state='unverified',current_bytes=None,root_present=None,error=type(error).__name__)
            # Event loop timers can fire within one tick of the deadline.
            if time.monotonic()>=self.budget.deadline or (deadline==self.budget.deadline and timeout.expired()):
                self.budget.trip('timeout','deadline')
            else:
                self.budget.trip('disk_unverified','storage')
        finally:
            if acquired:
                self.lock.release()

    async def run(self):
        while not self.budget.committed and self.budget.violation is None:
            await self.sample()
            await asyncio.sleep(get_settings().execution_storage_poll_seconds)


@asynccontextmanager
async def recovery(budget,case,attempt):
    """Measure this recovery node; never invent the previous node's peak usage."""
    if budget.storage_policy is None:
        yield
        return
    from .workflow_state import ExecutionConflict
    workspace=json.loads(attempt.input_manifest_json).get('workspace',{})
    expected='.vibeflow/attempts/'+attempt.id
    if (workspace.get('attempt_id')!=attempt.id or workspace.get('inputs_path')!=expected+'/inputs'
            or workspace.get('run_path')!=expected+'/run'):
        raise ExecutionConflict('recovery_required','Storage recovery has no bound attempt directory')
    watch=Watch(budget,storage._fs_path(case.s3_prefix,expected),allow_missing=storage._use_s3(),scope='recovery_node')
    budget.storage_watch=watch
    await watch.sample()
    monitor=asyncio.create_task(watch.run())
    try:yield
    finally:
        monitor.cancel()
        await finish_before_cancel(asyncio.gather(monitor,return_exceptions=True))
