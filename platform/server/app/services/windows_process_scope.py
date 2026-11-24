"""Owned Windows Job Object and process handles; never terminate by PID lookup."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
import time

from .process_identity_native import windows_identity

JOB_TIME_LIMIT = 0x00000004
PRESERVE_JOB_TIME = 0x00000040
JOB_MEMORY_LIMIT = 0x00000200


def _kernel():
    if os.name!='nt':
        raise OSError('Windows process scope requires Windows')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    signatures={
        'CreateJobObjectW':([ctypes.c_void_p,wintypes.LPCWSTR],wintypes.HANDLE),
        'SetInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD],wintypes.BOOL),
        'AssignProcessToJobObject':([wintypes.HANDLE,wintypes.HANDLE],wintypes.BOOL),
        'IsProcessInJob':([wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)],wintypes.BOOL),
        'TerminateJobObject':([wintypes.HANDLE,wintypes.UINT],wintypes.BOOL),
        'QueryInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)],wintypes.BOOL),
        'GetCurrentProcess':([],wintypes.HANDLE),
        'OpenProcess':([wintypes.DWORD,wintypes.BOOL,wintypes.DWORD],wintypes.HANDLE),
        'WaitForSingleObject':([wintypes.HANDLE,wintypes.DWORD],wintypes.DWORD),
        'DuplicateHandle':([wintypes.HANDLE,wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,wintypes.BOOL,wintypes.DWORD],wintypes.BOOL),
        'CloseHandle':([wintypes.HANDLE],wintypes.BOOL),
    }
    for name,(arguments,result) in signatures.items():
        function=getattr(kernel,name)
        function.argtypes=arguments
        function.restype=result
    return kernel


class BasicLimits(ctypes.Structure):
    _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),
        ('flags',wintypes.DWORD),('minimum_working_set',ctypes.c_size_t),
        ('maximum_working_set',ctypes.c_size_t),('active_process_limit',wintypes.DWORD),
        ('affinity',ctypes.c_size_t),('priority_class',wintypes.DWORD),('scheduling_class',wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class ExtendedLimits(ctypes.Structure):
    _fields_=[('basic',BasicLimits),('io',IoCounters),('process_memory',ctypes.c_size_t),
        ('job_memory',ctypes.c_size_t),('peak_process_memory',ctypes.c_size_t),('peak_job_memory',ctypes.c_size_t)]


class BasicAccounting(ctypes.Structure):
    _fields_=[(name,ctypes.c_int64) for name in ('user_time','kernel_time','period_user_time','period_kernel_time')]+[
        (name,wintypes.DWORD) for name in ('page_faults','total_processes','active_processes','terminated_processes')]


def _require(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


class ProcessHandle:
    """Duplicate the actual creation handle while the trusted launcher is gated."""
    def __init__(self,creation_handle):
        self.kernel=_kernel()
        destination=wintypes.HANDLE()
        current=self.kernel.GetCurrentProcess()
        _require(self.kernel.DuplicateHandle(current,creation_handle,current,ctypes.byref(destination),0,False,2))
        self.handle=destination.value

    @classmethod
    def query(cls,expected):
        """Open query/wait rights only, then prove the recorded birth identity."""
        from .process_identity_native import validate
        validate(expected)
        if expected['platform']!='windows':
            raise ValueError('Expected a Windows process')
        kernel=_kernel()
        original=_require(kernel.OpenProcess(0x1000|0x00100000,False,expected['pid']))
        try:
            result=cls(original)
        finally:
            kernel.CloseHandle(original)
        try:
            if result.identity()!=expected:
                raise ValueError('Queried process identity no longer matches')
            return result
        except BaseException:
            result.close()
            raise

    def identity(self,*,allow_exited=False):
        if self.handle is None:
            raise ValueError('Process creation handle is closed')
        return windows_identity(self.handle,allow_exited=allow_exited)

    def close(self):
        if self.handle is not None:
            _require(self.kernel.CloseHandle(self.handle))
            self.handle=None

    def wait_exit(self,timeout=5):
        if self.handle is None or not isinstance(timeout,(int,float)) or not 0<=timeout<=60:
            raise ValueError('Invalid process exit wait')
        result=self.kernel.WaitForSingleObject(self.handle,int(timeout*1000))
        if result==0x102:
            raise TimeoutError('Owned process has not exited')
        if result!=0:
            raise ctypes.WinError(ctypes.get_last_error())


class WindowsScope:
    """A fresh named scope. Closing its last handle kills only its members."""
    def __init__(self,nonce,*,observe=True):
        if not isinstance(nonce,str) or not re.fullmatch(r'[0-9a-f]{64}',nonce):
            raise ValueError('Invalid process scope nonce')
        if type(observe) is not bool:raise ValueError('Invalid process scope observer selection')
        self.kernel=_kernel()
        self.observer=None
        self.name='Local\\VibeFlow-'+nonce
        ctypes.set_last_error(0)
        self.handle=_require(self.kernel.CreateJobObjectW(None,self.name))
        # Never take ownership of a pre-existing named object, even with a nonce.
        if ctypes.get_last_error()==183:
            self.close()
            raise FileExistsError('Process scope already exists')
        try:
            limits=ExtendedLimits()
            limits.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _require(self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)))
            if observe:
                from .windows_process_lifetime import LifetimeObserver
                self.observer=LifetimeObserver(self)
        except BaseException:
            self.close()
            raise

    def contains(self,process):
        if self.handle is None or process.handle is None:
            raise ValueError('Process scope or handle is closed')
        member=wintypes.BOOL()
        _require(self.kernel.IsProcessInJob(process.handle,self.handle,ctypes.byref(member)))
        return bool(member.value)

    def assign(self,process,expected):
        if process.identity()!=expected:
            raise ValueError('Process creation identity does not match the launch handshake')
        _require(self.kernel.AssignProcessToJobObject(self.handle,process.handle))
        if not self.contains(process):
            raise ValueError('Process was not assigned to its owned scope')

    def terminate(self,exit_code=125):
        if self.handle is None:
            raise ValueError('Process scope is closed')
        if type(exit_code) is not int or not 0<=exit_code<=0xffffffff:
            raise ValueError('Invalid process termination code')
        _require(self.kernel.TerminateJobObject(self.handle,exit_code))

    def accounting(self):
        if self.handle is None:
            raise ValueError('Process scope is closed')
        info=BasicAccounting()
        _require(self.kernel.QueryInformationJobObject(self.handle,1,ctypes.byref(info),ctypes.sizeof(info),None))
        return info

    def active_processes(self):
        return int(self.accounting().active_processes)

    def _extended_limits(self):
        limits = ExtendedLimits()
        _require(self.kernel.QueryInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits), None))
        return limits

    def configure_compute(self, cpu_seconds, memory_bytes):
        if self.accounting().total_processes:
            raise ValueError('Compute limits must be set before admitting any process')
        limits = self._extended_limits()
        if cpu_seconds:
            limits.basic.flags |= JOB_TIME_LIMIT
            limits.basic.job_time = cpu_seconds * 10_000_000
        if memory_bytes:
            limits.basic.flags |= JOB_MEMORY_LIMIT
            limits.job_memory = memory_bytes
        _require(self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))

    def compute_usage(self):
        accounting = self.accounting()
        limits = self._extended_limits()
        state = self.kernel.WaitForSingleObject(self.handle, 0)
        if state not in (0, 0x102):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            'cpu_user_100ns': accounting.user_time,
            'cpu_kernel_100ns': accounting.kernel_time,
            'peak_job_memory_bytes': limits.peak_job_memory,
            'processes': accounting.total_processes,
            'active_processes': accounting.active_processes,
            'kernel_cpu_limit_reached': state == 0,
        }

    def wait_empty(self,timeout=5):
        if not isinstance(timeout,(int,float)) or not 0<=timeout<=60:
            raise ValueError('Invalid process scope wait timeout')
        deadline=time.monotonic()+timeout
        while self.active_processes():
            if time.monotonic()>=deadline:
                raise TimeoutError('Owned process scope did not exit before the deadline')
            time.sleep(.02)

    def retire(self,timeout=5):
        """Seal membership and reconcile all lifetime generations, including earlier exits."""
        if not isinstance(timeout,(int,float)) or not 0<=timeout<=60:
            raise ValueError('Invalid process scope wait timeout')
        handles=[]
        try:
            limits=self._extended_limits()
            if limits.basic.flags & JOB_TIME_LIMIT:
                limits.basic.flags = (limits.basic.flags & ~JOB_TIME_LIMIT) | PRESERVE_JOB_TIME
            limits.basic.flags|=0x8  # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            limits.basic.active_process_limit=0
            _require(self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)))
            capacity=16
            while True:
                class Members(ctypes.Structure):
                    _fields_=[('assigned',wintypes.DWORD),('returned',wintypes.DWORD),('pids',ctypes.c_size_t*capacity)]
                members=Members()
                ok=self.kernel.QueryInformationJobObject(self.handle,3,ctypes.byref(members),ctypes.sizeof(members),None)
                if ok and members.returned==members.assigned:
                    break
                if not ok and ctypes.get_last_error()!=234:
                    raise ctypes.WinError(ctypes.get_last_error())
                capacity=max(capacity*2,int(members.assigned))
                if capacity>4096:
                    raise RuntimeError('Process scope member observation exceeds its safety bound')
            for pid in members.pids[:members.returned]:
                raw=self.kernel.OpenProcess(0x1000|0x00100000,False,pid)
                if not raw:
                    if ctypes.get_last_error()==87:
                        continue  # The process object no longer exists.
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    process=ProcessHandle(raw)
                finally:
                    self.kernel.CloseHandle(raw)
                if self.contains(process):
                    handles.append(process)
                else:
                    # PID reuse must not turn a read-only observation into
                    # ownership. Never signal this unrelated process.
                    process.close()
            forced=bool(self.active_processes())
            if forced:
                self.terminate(125)
            deadline=time.monotonic()+timeout
            for process in handles:
                process.wait_exit(max(0,deadline-time.monotonic()))
            for process in handles:
                process.close()
            handles.clear()
            self.wait_empty(max(0,deadline-time.monotonic()))
            self.observer.finish(int(self.accounting().total_processes),max(0,deadline-time.monotonic()))
            return forced
        except BaseException:
            # Failed observation still stops the owned scope, but does not
            # claim complete exit verification.
            self.terminate(125)
            raise
        finally:
            for process in handles:
                process.close()

    def close(self):
        try:
            if self.observer is not None:
                self.observer.close()
        finally:
            if self.handle is not None:
                _require(self.kernel.CloseHandle(self.handle))
                self.handle=None
