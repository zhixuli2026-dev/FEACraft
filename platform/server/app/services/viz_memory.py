"""Sample the visualization worker's resident memory and the OS memory pool."""

import os
from pathlib import Path


def sample():
    if os.name == 'nt':
        return windows_sample()
    process = read_kib('/proc/self/status', {'VmRSS','VmHWM'})
    system = read_kib('/proc/meminfo', {'MemTotal','MemAvailable'})
    return {'process_kib': process['VmRSS'], 'peak_kib': process['VmHWM'],
            'total_kib': system['MemTotal'], 'available_kib': system['MemAvailable']}


def read_kib(path, names):
    result = {}
    for line in Path(path).read_text(encoding='ascii').splitlines():
        key, _, tail = line.partition(':')
        if key in names:
            result[key] = int(tail.split()[0])
    if result.keys() != names:
        raise OSError('The operating system did not return all memory counters')
    return result


def windows_sample():
    import ctypes as c
    from ctypes import wintypes as win

    class ProcessMemory(c.Structure):
        _fields_ = [('size', win.DWORD), ('faults', win.DWORD), ('peak_working_set', c.c_size_t),
                    ('working_set', c.c_size_t), ('pool_and_commit', c.c_size_t*6)]

    class SystemMemory(c.Structure):
        _fields_ = [('size', win.DWORD), ('load', win.DWORD), ('total', c.c_ulonglong),
                    ('available', c.c_ulonglong), ('commit_and_virtual', c.c_ulonglong*5)]

    kernel = c.WinDLL('kernel32', use_last_error=True)
    current = kernel.GetCurrentProcess
    current.restype = win.HANDLE
    process_memory = kernel.K32GetProcessMemoryInfo
    process_memory.argtypes = [win.HANDLE, c.POINTER(ProcessMemory), win.DWORD]
    process_memory.restype = win.BOOL
    system_memory = kernel.GlobalMemoryStatusEx
    system_memory.argtypes = [c.POINTER(SystemMemory)]
    system_memory.restype = win.BOOL
    process, system = ProcessMemory(), SystemMemory()
    process.size, system.size = c.sizeof(process), c.sizeof(system)
    if not process_memory(current(), c.byref(process), process.size) or not system_memory(c.byref(system)):
        raise c.WinError(c.get_last_error())
    return {'process_kib': process.working_set//1024, 'peak_kib': process.peak_working_set//1024,
            'total_kib': system.total//1024, 'available_kib': system.available//1024}
