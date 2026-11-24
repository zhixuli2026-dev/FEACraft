"""Create the fixed host gate inside its Job before its first thread runs.

Uses PROC_THREAD_ATTRIBUTE_JOB_LIST and an explicit stdio handle allowlist.
The adapter retains CPython's overlapped pipes and Proactor process waiting;
there is no global subprocess patch or suspended/unassigned fallback.
"""
from __future__ import annotations

import asyncio
from asyncio import windows_events, windows_utils
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys

from .windows_process_scope import _kernel, _require


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('reserved', wintypes.LPWSTR),
                ('desktop', wintypes.LPWSTR), ('title', wintypes.LPWSTR)] + [
        (name, wintypes.DWORD) for name in ('x', 'y', 'width', 'height', 'chars_x', 'chars_y', 'fill', 'flags')
    ] + [('show', wintypes.WORD), ('reserved_size', wintypes.WORD), ('reserved_bytes', ctypes.c_void_p),
         ('stdin', wintypes.HANDLE), ('stdout', wintypes.HANDLE), ('stderr', wintypes.HANDLE)]


class ExtendedStartup(ctypes.Structure):
    _fields_ = [('startup', StartupInfo), ('attributes', ctypes.c_void_p)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('process', wintypes.HANDLE), ('thread', wintypes.HANDLE),
                ('pid', wintypes.DWORD), ('tid', wintypes.DWORD)]


def _environment(values):
    if values is None:
        return None
    rows = []
    for key, value in values.items():
        if (not isinstance(key, str) or not isinstance(value, str) or not key or
                '\0' in key or '\0' in value or '=' in key[1:]):
            raise ValueError('Invalid owned process environment')
        rows.append(key + '=' + value)
    rows.sort(key=str.upper)
    return ctypes.create_unicode_buffer('\0'.join(rows) + '\0\0')


def _create_process(args, scope, stdio, cwd, env, creationflags, parent=None):
    if (not isinstance(args, (list, tuple)) or not args or
            any(not isinstance(value, str) or '\0' in value for value in args) or
            not os.path.isabs(args[0])):
        raise ValueError('Owned host requires an absolute executable and unchanged argv')
    if scope.handle is None or creationflags not in (0, subprocess.CREATE_NEW_CONSOLE):
        raise ValueError('Invalid owned Job or process creation flags')
    if parent is not None and (parent is scope or parent.handle is None):
        raise ValueError('Invalid parent Windows Job')
    if len(stdio) != 3 or any(type(handle) is not int or handle <= 0 for handle in stdio):
        raise ValueError('Owned host requires three actual stdio handles')
    if cwd is not None and (not isinstance(cwd, str) or '\0' in cwd or not os.path.isabs(cwd)):
        raise ValueError('Owned process cwd must be absolute')
    command = subprocess.list2cmdline(args)
    if len(command) >= 32767:
        raise ValueError('Owned host command exceeds Windows limit')
    kernel = _kernel()
    signatures = {
        'InitializeProcThreadAttributeList': ([ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                               ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        'UpdateProcThreadAttribute': ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        'DeleteProcThreadAttributeList': ([ctypes.c_void_p], None),
        'CreateProcessW': ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                            ctypes.POINTER(ExtendedStartup), ctypes.POINTER(ProcessInfo)], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    size = ctypes.c_size_t()
    ok = kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
    if ok or ctypes.get_last_error() != 122 or not 0 < size.value <= 65536:
        raise OSError('Cannot size owned process attributes')
    attributes = ctypes.create_string_buffer(size.value)
    _require(kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)))
    try:
        job_handles = [scope.handle] if parent is None else [parent.handle, scope.handle]
        jobs = (wintypes.HANDLE * len(job_handles))(*job_handles)
        handles = (wintypes.HANDLE * 3)(*stdio)
        # JOB_LIST and HANDLE_LIST; the Job handle itself is never inherited.
        _require(kernel.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, ctypes.sizeof(jobs), None, None))
        _require(kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None))
        startup = ExtendedStartup()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x100 | 0x1  # USESTDHANDLES | USESHOWWINDOW
        startup.startup.show = 0  # SW_HIDE
        startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = stdio
        startup.attributes = ctypes.addressof(attributes)
        info = ProcessInfo()
        environment = _environment(env)
        mutable_command = ctypes.create_unicode_buffer(command)
        sys.audit('subprocess.Popen', args[0], command, cwd, env)
        _require(kernel.CreateProcessW(args[0], mutable_command, None, None, True,
                 creationflags | 0x80000 | 0x400,  # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
                 environment, cwd, ctypes.byref(startup), ctypes.byref(info)))
        return info.process, info.thread, info.pid
    finally:
        kernel.DeleteProcThreadAttributeList(attributes)


class JobPopen(windows_utils.Popen):
    """Only the fixed raw-stdio host gate uses this CPython Windows adapter."""
    def __init__(self, args, *, job_scope, parent_job_scope=None, **kwargs):
        self._job_scope = job_scope
        self._parent_job_scope = parent_job_scope
        super().__init__(args, **kwargs)

    def _execute_child(self, args, executable, preexec_fn, close_fds,
                       pass_fds, cwd, env, startupinfo, creationflags, shell,
                       p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
                       unused_restore_signals, unused_gid, unused_gids, unused_uid,
                       unused_umask, unused_start_new_session, unused_process_group):
        thread = None
        try:
            if (executable is not None or preexec_fn is not None or not close_fds or pass_fds or shell
                    or startupinfo is not None or unused_start_new_session
                    or any(value is not None for value in (unused_gid, unused_gids, unused_uid))
                    or unused_umask != -1 or unused_process_group != -1):
                raise ValueError('Unsupported option for owned host creation')
            process, thread, pid = _create_process(args, self._job_scope,
                [int(p2cread), int(c2pwrite), int(errwrite)], cwd, env, creationflags, self._parent_job_scope)
            self._handle = subprocess.Handle(process)
            self.pid = pid
            self._child_created = True
        finally:
            if thread is not None:
                _require(_kernel().CloseHandle(thread))
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)


class JobTransport(windows_events._WindowsSubprocessTransport):
    def _start(self, args, shell, stdin, stdout, stderr, bufsize, **kwargs):
        self._proc = JobPopen(args, shell=shell, stdin=stdin, stdout=stdout,
                              stderr=stderr, bufsize=bufsize, **kwargs)
        def completed(_):
            self._process_exited(self._proc.poll())
        waiting = self._loop._proactor.wait_for_handle(int(self._proc._handle))
        waiting.add_done_callback(completed)


async def create(scope, *args, cwd=None, env=None, parent_scope=None):
    """Return a normal asyncio Process; all creation is inside the owned Job."""
    loop = asyncio.get_running_loop()
    if not isinstance(loop, windows_events.ProactorEventLoop):
        raise ValueError('Owned Windows launch requires the Proactor event loop')
    protocol = asyncio.subprocess.SubprocessStreamProtocol(limit=65536, loop=loop)
    waiter = loop.create_future()
    transport = JobTransport(loop, protocol, args, False,
        subprocess.PIPE, subprocess.PIPE, subprocess.PIPE, 0, waiter=waiter,
        job_scope=scope, parent_job_scope=parent_scope, cwd=cwd, env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        await waiter
    except BaseException:
        transport.close()
        await transport._wait()
        raise
    return asyncio.subprocess.Process(transport, protocol, loop)
