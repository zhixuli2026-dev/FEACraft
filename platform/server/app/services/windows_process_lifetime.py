"""Observe Job membership from before launch; reconcile notifications with accounting.

Notifications are hints, never authority to signal a PID. Retained query/wait
handles prove exits; missing or ambiguous notifications make verification fail.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time


class Association(ctypes.Structure):
    _fields_=[('key',ctypes.c_void_p),('port',wintypes.HANDLE)]


class LifetimeObserver:
    MAX_GENERATIONS=65536
    MAX_PENDING=4096

    def __init__(self,scope):
        from .windows_process_scope import _kernel,_require
        self.scope=scope
        self.kernel=_kernel()
        self.kernel.CreateIoCompletionPort.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.c_size_t,wintypes.DWORD]
        self.kernel.CreateIoCompletionPort.restype=wintypes.HANDLE
        self.kernel.GetQueuedCompletionStatus.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_void_p),wintypes.DWORD]
        self.kernel.GetQueuedCompletionStatus.restype=wintypes.BOOL
        self.lock=threading.RLock()
        self.shutdown=threading.Event()
        self.port=None
        self.thread=None
        self.seen=set()
        self.pids=set()
        self.gone=set()
        self.pending={}
        self.exited=0
        self.error=None
        self.total=None
        self.complete=False
        try:
            self.port=_require(self.kernel.CreateIoCompletionPort(wintypes.HANDLE(-1),None,0,1))
            association=Association(1,self.port)
            _require(self.kernel.SetInformationJobObject(scope.handle,7,ctypes.byref(association),ctypes.sizeof(association)))
            self.thread=threading.Thread(target=self._monitor,name='vibeflow-job-members',daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _missing(self,pid):
        # A delayed notification can refer to a deleted process or an unrelated
        # generation. It proves only the former member is gone. Repeated IDs
        # without a captured birth cannot distinguish duplicate/reused messages.
        if pid in self.pids or pid in self.gone:
            raise RuntimeError('Job membership generation is ambiguous')
        self.gone.add(pid)

    def _capture(self,pid):
        from .windows_process_scope import ProcessHandle
        if type(pid) is not int or not 0<pid<=0xffffffff:
            raise ValueError('Invalid Job process notification')
        if len(self.seen)+len(self.gone)>=self.MAX_GENERATIONS:
            raise RuntimeError('Job lifetime observation exceeds its bound')
        raw=self.kernel.OpenProcess(0x1000|0x00100000,False,pid)
        if not raw:
            if ctypes.get_last_error()==87:
                self._missing(pid)
                return
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            process=ProcessHandle(raw)
        finally:
            self.kernel.CloseHandle(raw)
        try:
            if not self.scope.contains(process):
                # A rejected association or a recycled PID may name a live
                # non-member. Its notification alone cannot prove an exit.
                # Observe it read-only; never signal an unrelated generation.
                try:
                    process.wait_exit(0)
                except TimeoutError:
                    raise RuntimeError('Job notification names an unverified live generation') from None
                self._missing(pid)
                return
            # Membership was checked on this retained query/wait handle. The
            # member may exit before its birth is read; GetProcessTimes still
            # identifies that exact object and the same handle proves its exit.
            observed=process.identity(allow_exited=True)
            key=(observed['pid'],observed['creation_time_100ns'])
            if key in self.seen:
                return  # Duplicate/nested notifications cannot fill a count gap.
            if pid in self.gone:
                raise RuntimeError('Job membership generation is ambiguous')
            if len(self.pending)>=self.MAX_PENDING:
                raise RuntimeError('Too many unverified Job member exits')
            self.seen.add(key)
            self.pids.add(pid)
            self.pending[key]=process
            process=None
        finally:
            if process is not None: process.close()

    def _reap(self):
        for key,process in list(self.pending.items()):
            try:
                process.wait_exit(0)
            except TimeoutError:
                continue
            process.close()
            del self.pending[key]
            self.exited+=1

    def _monitor(self):
        next_reap=0
        while not self.shutdown.is_set():
            code=wintypes.DWORD();key=ctypes.c_size_t();pid=ctypes.c_void_p()
            try:
                ok=self.kernel.GetQueuedCompletionStatus(self.port,ctypes.byref(code),ctypes.byref(key),ctypes.byref(pid),50)
                if not ok and ctypes.get_last_error()!=258:
                    raise ctypes.WinError(ctypes.get_last_error())
                with self.lock:
                    if self.error is not None:
                        continue  # Drain the OS queue without growing evidence.
                    if ok:
                        if key.value!=1: raise ValueError('Unexpected Job completion key')
                        if code.value==6: self._capture(pid.value)
                    if time.monotonic()>=next_reap:
                        self._reap()
                        next_reap=time.monotonic()+.05
            except BaseException as error:
                with self.lock:
                    self.error=self.error or type(error).__name__
                time.sleep(.05)

    def finish(self,total,timeout):
        if type(total) is not int or not 0<=total<=self.MAX_GENERATIONS:
            raise ValueError('Invalid Job lifetime accounting')
        deadline=time.monotonic()+timeout
        while True:
            with self.lock:
                self.total=total
                if self.error is not None:
                    raise RuntimeError('Job lifetime observation failed: '+self.error)
                observed=len(self.seen)+len(self.gone)
                if observed>total:
                    raise RuntimeError('Job lifetime observations exceed accounting')
                if observed==total and not self.pending:
                    self.complete=True
                    return
            if time.monotonic()>=deadline:
                raise TimeoutError('Job lifetime notifications or exit handles are incomplete')
            time.sleep(.02)

    def summary(self):
        with self.lock:
            return {'schema':'vibeflow.windows-members.v1','total':self.total,
                'observed':len(self.seen)+len(self.gone),'exit_verified':self.exited,
                'gone_before_open':len(self.gone),'pending':len(self.pending),
                'complete':self.complete,'error':self.error}

    def close(self):
        self.shutdown.set()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(2)
            if self.thread.is_alive(): raise TimeoutError('Job member observer did not close')
        try:
            for process in list(self.pending.values()): process.close()
            self.pending.clear()
        finally:
            if self.port is not None:
                from .windows_process_scope import _require
                _require(self.kernel.CloseHandle(self.port))
                self.port=None
