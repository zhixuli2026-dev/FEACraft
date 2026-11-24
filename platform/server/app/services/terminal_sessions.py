"""Interactive session lifetime: persist, register, release, pump and verified close."""
import asyncio
import base64
import codecs
import copy
import json
import time
from collections import deque

from ..config import get_settings
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .execution_budget import Budget
from .owned_terminal import OwnedTerminal
from .terminal_output import OutputQueue
from . import terminal_records as records, terminal_completion

RESOURCE_LIMIT_ERROR='TerminalResourceLimit'
RESOURCE_LIMIT_MESSAGES={
    'timeout':'终端已达到运行时限',
    'log_limit':'终端已达到累计输出限额',
    'disk_limit':'终端工作目录用量或所在卷可用空间已达到限制',
    'disk_unverified':'无法核验终端工作目录的磁盘用量',
}


def resource_limit_reason(error,stop):
    if error==RESOURCE_LIMIT_ERROR and stop is not None and stop['verified'] and stop['reason'] in RESOURCE_LIMIT_MESSAGES:
        return stop['reason']
    return None


class TerminalSession:
    def __init__(self,case_id,user_id,source_prefix=None,staging_prefix=None):
        self.case_id=case_id or None;self.user_id=user_id;self.source_prefix=source_prefix;self.staging_prefix=staging_prefix
        self.transport=OwnedTerminal();self.owner=None;self.id=None;self.shell=None;self.alive=None
        self._buf=deque();self._buf_bytes=0;self._subscribers=[];self.last_activity=time.time()
        self._pump_task=None;self._watch_task=None;self._heartbeat_task=None;self._close_task=None
        self._storage_task=None
        self._pending_completion=None
        self._input_task=None;self._input_queue=asyncio.Queue(maxsize=1024);self.pending_input_bytes=0
        self._pending_resize=None;self._resize_wakeup=False
        self.cleanup_verified=None;self.persisted_terminal=False;self.error=None;self.exit_code=None;self._notified=False
        self.finalize=None;self.on_closed=None
        self.exporter=None;self.export_receipt=None;self._export_deadline_task=None
        self._budget=None;self.resource_limit_reason=None

    async def start(self,*,cwd,cols,rows,shell,distro,reauthorize,finalize=None,credential=None,environment=None):
        self.shell=shell;self.finalize=finalize
        settings=get_settings()
        lifetime=settings.terminal_session_lifetime_seconds
        output_limit=settings.terminal_session_max_output_bytes
        ordinary=shell in {'cmd','wsl'}
        async def allocate():
            self.owner=await records.create(self.case_id,self.user_id,selected);self.id=self.owner.id
        try:
            disk_root=None
            if shell=='wsl-sandbox':
                from .sandbox_export_execution import SandboxExport,LIFETIME
                self.exporter=SandboxExport(None)
                disk_root=await self.exporter.reserve_image()
            selected=self.transport.configure(shell,cwd=cwd,cols=cols,rows=rows,distro=distro,disk_root=disk_root)
            selected.update(source_prefix=self.source_prefix,staging_prefix=self.staging_prefix)
            if credential is not None:selected['credential']=credential
            workspace_limit=settings.terminal_session_workspace_limit_mb
            if ordinary and workspace_limit and self.case_id is not None:
                from . import execution_storage
                if cwd is None:raise ValueError('Case-bound terminal has no working directory')
                # Admission uses the same bounded sampler and root identity as
                # running checks. No process is registered or released yet.
                self._budget=Budget(self.transport.owned.nonce,float('inf'),output_limit)
                self._budget.storage_policy=execution_storage.validate({
                    'schema':execution_storage.SCHEMA,'max_workspace_bytes':workspace_limit*1024*1024,
                    'min_free_bytes':settings.node_storage_min_free_mb*1024*1024,
                })
                watch=execution_storage.Watch(self._budget,cwd,scope='terminal_workspace')
                self._budget.storage_watch=watch
                await self._check_storage_admission()
            await finish_before_cancel(allocate())
            self._heartbeat_task=asyncio.create_task(self._renew())
            root=await self.transport.prepare_configured(environment);await records.register_root(self.owner,root)
            await self.transport.start_bridge();await records.register_gate(self.owner,self.transport)
            if self.exporter is not None:
                # The registered outer gate completed the fixed-size image
                # before the bridge handshake. Its bytes now count physically.
                await self.exporter.release_image_reservation()
            await reauthorize()
            if self._budget is not None:
                await self._check_storage_admission()
            await self.transport.release()
            if ordinary and (lifetime or output_limit or self._budget is not None):
                deadline=time.monotonic()+lifetime if lifetime else float('inf')
                if self._budget is None:
                    self._budget=Budget(self.id,deadline,output_limit)
                else:
                    self._budget.attempt_id=self.id;self._budget.deadline=deadline
                    self._storage_task=asyncio.create_task(self._budget.storage_watch.run())
            self.alive=True;self._pump_task=asyncio.create_task(self._pump())
            if shell=='wsl-sandbox':
                self.exporter.owner=self.owner
                await self.exporter.start()
                async def deadline():
                    await asyncio.sleep(LIFETIME-180)
                    await self.close('timeout')
                self._export_deadline_task=asyncio.create_task(deadline())
            self._input_task=asyncio.create_task(self._feed_input());self._watch_task=asyncio.create_task(self._watch())
            return self
        except BaseException as error:
            reason=getattr(error,'code',None)
            storage_failure=reason in {'terminal_disk_limit','terminal_disk_unverified'}
            try:await finish_before_cancel(self.close(reason.removeprefix('terminal_') if storage_failure else 'executor_error',
                error=RESOURCE_LIMIT_ERROR if storage_failure else type(error).__name__))
            except BaseException as cleanup_error:error.add_note('Terminal startup cleanup: '+type(cleanup_error).__name__)
            raise

    async def _check_storage_admission(self):
        from .execution_storage import limit_message
        await self._budget.storage_watch.sample()
        reason=self._budget.violation
        if reason is not None:
            message=(limit_message(self._budget.storage_observation) if reason=='disk_limit'
                     else RESOURCE_LIMIT_MESSAGES[reason])
            raise CaseWriteConflict('terminal_'+reason,message)

    @property
    def release_attempted(self):
        return self.transport.release_attempted

    async def _renew(self):
        try:
            while True:
                await asyncio.sleep(5);await records.heartbeat(self.owner)
        except asyncio.CancelledError:raise
        except Exception as error:
            closing=asyncio.create_task(self.close('ownership_lost',error=type(error).__name__))
            closing.add_done_callback(lambda task:task.exception() if not task.cancelled() else None)

    async def _pump(self):
        decoder=codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                frame=await self.transport.receive()
                if frame.get('type')=='output' and set(frame)=={'type','data'}:
                    data=base64.b64decode(frame['data'],validate=True)
                    if len(data)>16*1024:raise ValueError('Terminal output frame exceeds its limit')
                    if self._budget is not None and self._budget.max_log_bytes:
                        allowed=self._budget.consume_output(len(data),'root')
                        if allowed<len(data):
                            self._append(decoder.decode(data[:allowed]))
                            return
                    self._append(decoder.decode(data))
                elif frame.get('type')=='exit' and set(frame)=={'type','code'} and type(frame['code']) is int:
                    if not -(2**31)<=frame['code']<2**32:raise ValueError('Invalid terminal exit status')
                    self.exit_code=frame['code']-(2**32) if frame['code']>=2**31 else frame['code'];return
                elif frame.get('type')=='input_ack' and set(frame)=={'type','id'}:
                    self.transport.acknowledge_input(frame['id']);self.last_activity=time.time()
                elif frame.get('type')=='resize_ack' and set(frame)=={'type','id'}:
                    self.transport.acknowledge_input(frame['id'],'resize');self.last_activity=time.time()
                else:raise ValueError('Invalid terminal output')
        except (EOFError,OSError):
            if self._close_task is None:raise
        finally:
            self.transport.fail_inputs();self._append(decoder.decode(b'',final=True))

    def _append(self,data):
        if not data:return
        self.last_activity=time.time()
        tail=data.encode('utf8')[-records.TAIL_BYTES:].decode('utf8','ignore')
        self._buf.append(tail);self._buf_bytes+=len(tail.encode('utf8'))
        while self._buf_bytes>records.TAIL_BYTES or len(self._buf)>1024:
            self._buf_bytes-=len(self._buf.popleft().encode('utf8'))
        for queue in tuple(self._subscribers):queue.put_nowait(data)

    async def _watch(self):
        failure=None
        tasks=[self._pump_task,self._input_task]
        if self.exporter is not None:tasks.append(self.exporter.task)
        if self._storage_task is not None:tasks.append(self._storage_task)
        deadline=self._budget.remaining() if self._budget is not None and self._budget.deadline!=float('inf') else None
        done,_=await asyncio.wait(tasks,timeout=deadline,return_when=asyncio.FIRST_COMPLETED)
        if self._budget is not None:
            if not done:self._budget.trip('timeout','deadline')
            if self._budget.violation is not None:
                try:await self.close(self._budget.violation,error=RESOURCE_LIMIT_ERROR)
                except Exception:pass  # Existing closure path retains and reports the failure.
                return
        if self.exporter is not None and self.exporter.task.done() and self._close_task is None:
            try:await self.close('executor_error',error='SandboxExportEnded')
            except Exception:pass
            return
        task=next(task for task in tasks if task.done())
        try:task.result()
        except asyncio.CancelledError:
            if self._close_task is None:failure='CancelledError'
        except Exception as error:failure=type(error).__name__
        try:await self.close('executor_error' if failure else 'root_exit',error=failure)
        except Exception:pass  # Durable record / self.error and WS error retain the failure.

    def subscribe(self):
        queue=OutputQueue();snapshot=''.join(self._buf)
        if self._notified:queue.put_nowait(None)
        else:self._subscribers.append(queue)
        return queue,snapshot

    def unsubscribe(self,queue):
        if queue in self._subscribers:self._subscribers.remove(queue)

    async def write(self,data):
        if self.alive is not True or self._close_task is not None:return
        data=data.encode('utf8','replace').decode('utf8');size=len(data.encode('utf8'))
        if not size:return True
        if self.pending_input_bytes+size>2*1024*1024 or self._input_queue.full():return False
        self.pending_input_bytes+=size;self._input_queue.put_nowait((data,size));self.last_activity=time.time()
        return True

    async def _feed_input(self):
        while True:
            item=await self._input_queue.get()
            if item is None:self._resize_wakeup=False
            if self._pending_resize is not None:
                cols,rows=self._pending_resize;self._pending_resize=None
                await self.transport.resize(cols,rows)
            if item is None:continue
            data,size=item
            try:await self.transport.write(data)
            finally:self.pending_input_bytes-=size

    async def resize(self,cols,rows):
        if self.alive is True and self._close_task is None:
            self._pending_resize=(cols,rows)
            if not self._resize_wakeup and not self._input_queue.full():
                self._resize_wakeup=True;self._input_queue.put_nowait(None)

    async def close(self,reason='cancelled',*,error=None):
        if self._close_task is None:self._close_task=asyncio.create_task(self._close(reason,error))
        elif (self._close_task.done() and not self.persisted_terminal
                and self.cleanup_verified is True and self._pending_completion is not None):
            # Only the original owner's retained completion can be retried.
            # Never repeat process control, export or staging synchronization.
            self._close_task=asyncio.create_task(self._retry_completion())
        return await finish_before_cancel(self._close_task)

    async def _persist_completion(self):
        owner,payload=self._pending_completion
        await terminal_completion.commit(owner,copy.deepcopy(payload))
        self.persisted_terminal=True;self.error=payload['error']
        self.resource_limit_reason=resource_limit_reason(self.error,payload['stop'])

    async def _retry_completion(self):
        try:
            await self._persist_completion()
            if self.error:raise RuntimeError('Terminal closure failed: '+self.error)
            return self._pending_completion[1]['stop']
        except BaseException as error:
            self.error=self.error or type(error).__name__
            raise
        finally:
            if self.persisted_terminal and self.cleanup_verified is True and self.on_closed is not None:self.on_closed(self)

    async def _close(self,reason,error):
        failure=error;stop=None
        try:
            if self._input_task is not None:
                self._input_task.cancel();await asyncio.gather(self._input_task,return_exceptions=True)
            while not self._input_queue.empty():self._input_queue.get_nowait()
            self.pending_input_bytes=0
            self._pending_resize=None;self._resize_wakeup=False
            try:
                if self.transport.owned.closed:stop=self.transport.owned.stop_result
                elif self.transport.owned.proc is not None:stop=await self.transport.stop(reason)
                else:await self.transport.abort()
            except BaseException as cleanup_error:
                stop=self.transport.owned.stop_result;failure=failure or type(cleanup_error).__name__
            self.cleanup_verified=stop.get('verified') if stop is not None else None
            if self.cleanup_verified is True:self.alive=False
            if self.cleanup_verified is not True:failure=failure or 'RuntimeError'
            if self._storage_task is not None:
                self._storage_task.cancel()
                await asyncio.gather(self._storage_task,return_exceptions=True)
            if self._pump_task is not None:
                try:await asyncio.wait_for(asyncio.shield(self._pump_task),5)
                except BaseException as pump_error:
                    if not self._pump_task.done():self._pump_task.cancel();await asyncio.gather(self._pump_task,return_exceptions=True)
                    if not isinstance(pump_error,(EOFError,OSError,asyncio.CancelledError)):failure=failure or type(pump_error).__name__
            if self.exporter is not None:
                if self.release_attempted and self.cleanup_verified:
                    try:await self.exporter.export(self.transport.owned.record,stop)
                    except Exception as export_error:failure=failure or type(export_error).__name__
                elif not self.release_attempted and (self.cleanup_verified or self.transport.owned.proc is None):
                    try:await self.exporter.discard_unreleased(self.transport,stop)
                    except Exception:failure='SandboxStartupImageCleanupIncomplete'
            if self.cleanup_verified and (self.exporter is None or self.exporter.manifest is not None) and self.finalize is not None:
                try:await self.finalize(self)
                except Exception as staging_error:failure=failure or type(staging_error).__name__
            if self.owner is not None:
                if self.exporter is not None and self.export_receipt is None:failure=failure or 'SandboxPublicationIncomplete'
                root=self.transport.owned.record
                self._pending_completion=(self.owner,copy.deepcopy(dict(root=root,stop=stop if root is not None else None,
                    tail=''.join(self._buf).encode('utf8')[-records.TAIL_BYTES:].decode('utf8','ignore'),
                    exit_code=self.exit_code,error=failure or (None if self.cleanup_verified else 'RuntimeError'),
                    export=self.export_receipt)))
                await self._persist_completion()
            self.error=failure
            if failure:raise RuntimeError('Terminal closure failed: '+failure)
            return stop
        except BaseException as error:
            self.error=self.error or type(error).__name__
            raise
        finally:
            if self.exporter is not None:await finish_before_cancel(self.exporter.abort())
            if self.exporter is not None and (self.cleanup_verified is True or self.transport.owned.proc is None):
                await self.exporter.release_image_reservation()
            if self._export_deadline_task is not None:
                self._export_deadline_task.cancel()
            self.alive=False if self.cleanup_verified is True else None
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel();await asyncio.gather(self._heartbeat_task,return_exceptions=True)
            if not self._notified:
                self._notified=True
                for queue in tuple(self._subscribers):queue.put_nowait(None)
            if self.persisted_terminal and self.cleanup_verified is True and self.on_closed is not None:self.on_closed(self)


class ClosedTerminal:
    """Replay a persisted terminal tail without creating or signaling a process."""
    def __init__(self,row):
        value=records.public(row)
        if value['phase'] not in {'closed','failed'}:raise ValueError('Terminal cannot be replayed from incomplete evidence')
        self.id=row.id;self.user_id=row.created_by;self.case_id=row.case_id;self.shell=row.shell
        self.cleanup_verified=value['cleanup_verified'];self.alive=False if self.cleanup_verified is True else None
        self.persisted_terminal=True;self.error=row.error
        self.resource_limit_reason=resource_limit_reason(self.error,value['stop'])
        self.exit_code=row.exit_code;self.tail=value['tail'] or '';self.last_activity=time.time();self._subscribers=[]

    def subscribe(self):
        queue=OutputQueue();queue.put_nowait(None);return queue,self.tail

    def unsubscribe(self,queue):pass
    async def write(self,data):pass
    async def resize(self,cols,rows):pass
