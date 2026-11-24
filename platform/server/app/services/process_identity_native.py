"""Read-only OS process identity. Also runs under the distribution's Python.

This module deliberately imports only the standard library: WSL probes must not
depend on the API virtualenv, database credentials or application configuration.
An identity match is evidence for a supervisor, never permission to kill a PID.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

SCHEMA='vibeflow.os-process.v1'
STATES={'match','exited','mismatch','unverified'}


@lru_cache(maxsize=1)
def host_id():
    if os.name=='nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Cryptography',
                0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as key:
            value=winreg.QueryValueEx(key,'MachineGuid')[0]
        value=str(uuid.UUID(value))
        namespace='windows'
    elif sys.platform.startswith('linux'):
        # Minimal WSL distributions may intentionally have an uninitialized
        # machine-id. A boot-scoped identity still refuses cross-boot reuse; the
        # enclosing Windows host/distro binding supplies the durable host scope.
        try:
            value=Path('/etc/machine-id').read_text(encoding='ascii').strip()
        except FileNotFoundError:
            value=''
        if re.fullmatch(r'[0-9a-f]{32}',value) and value!='0'*32:
            namespace='linux'
        else:
            value=str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()))
            namespace='linux-boot'
    else:
        raise OSError('Process identity is unsupported on this operating system')
    return hashlib.sha256((namespace+':'+value).encode('ascii')).hexdigest()


def _positive(value):
    return type(value) is int and 0<value<=0xffffffff


def validate(record):
    if (not isinstance(record,dict) or record.get('schema')!=SCHEMA or
            not _positive(record.get('pid')) or not isinstance(record.get('host_id'),str) or
            not re.fullmatch(r'[0-9a-f]{64}',record['host_id'])):
        raise ValueError('Invalid process identity')
    common={'schema','platform','host_id','pid'}
    if record.get('platform')=='windows':
        if set(record)!=common|{'creation_time_100ns'}:
            raise ValueError('Unexpected Windows process identity fields')
        value=record['creation_time_100ns']
        if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',value) or int(value)>2**64-1:
            raise ValueError('Invalid Windows creation time')
    elif record.get('platform')=='linux':
        if set(record)!=common|{'boot_id','start_ticks','process_group','session','uid','pid_namespace'}:
            raise ValueError('Unexpected Linux process identity fields')
        if (not isinstance(record['boot_id'],str) or str(uuid.UUID(record['boot_id']))!=record['boot_id'] or
                not _positive(record['process_group']) or not _positive(record['session']) or
                type(record['uid']) is not int or not 0<=record['uid']<=0xffffffff):
            raise ValueError('Invalid Linux execution scope')
        for key in ('start_ticks','pid_namespace'):
            if not isinstance(record[key],str) or not re.fullmatch(r'[1-9][0-9]{0,19}',record[key]):
                raise ValueError('Invalid Linux birth or namespace identity')
    else:
        raise ValueError('Unsupported process identity platform')
    return record


def linux_identity(pid):
    if not _positive(pid):
        raise ValueError('Invalid process ID')
    root=Path('/proc')/str(pid)
    # proc/stat field 2 can contain spaces, newlines and closing parentheses.
    raw=(root/'stat').read_text(encoding='utf-8')
    fields=raw.rsplit(')',1)[1].split()
    if fields[0] in {'Z','X','x'}:
        raise ProcessLookupError('Process has exited')
    if int(raw.split(' ',1)[0])!=pid:
        raise ValueError('Process identity changed')
    namespace=(root/'ns/pid').stat().st_ino
    uid=root.stat().st_uid
    result={'schema':SCHEMA,'platform':'linux','host_id':host_id(),'pid':pid,
        'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip(),
        'start_ticks':fields[19],'process_group':int(fields[2]),'session':int(fields[3]),
        'uid':uid,'pid_namespace':str(namespace)}
    # Re-read the birth marker after the other /proc reads. A disappearing or
    # replaced process must not be assembled into an apparently valid identity.
    again=(root/'stat').read_text(encoding='utf-8').rsplit(')',1)[1].split()
    if again[0] in {'Z','X','x'}:
        raise ProcessLookupError('Process has exited')
    if again[19]!=fields[19] or again[2:4]!=fields[2:4]:
        raise ValueError('Process identity changed during observation')
    return validate(result)


def windows_identity(handle,*,allow_exited=False):
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetProcessId.argtypes=[wintypes.HANDLE]
    kernel.GetProcessId.restype=wintypes.DWORD
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    kernel.GetProcessTimes.restype=wintypes.BOOL
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    state=kernel.WaitForSingleObject(handle,0)
    if state==0 and not allow_exited:
        raise ProcessLookupError('Process has exited')
    if state not in {0,0x102}:
        raise ctypes.WinError(ctypes.get_last_error())
    values=[wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle,*(ctypes.byref(value) for value in values)):
        raise ctypes.WinError(ctypes.get_last_error())
    pid=kernel.GetProcessId(handle)
    if not pid:
        raise ctypes.WinError(ctypes.get_last_error())
    creation=(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime
    return validate({'schema':SCHEMA,'platform':'windows','host_id':host_id(),
        'pid':int(pid),'creation_time_100ns':str(creation)})


def observe(pid):
    """Read-only observation; the caller must compare with a known birth marker."""
    if not _positive(pid):
        raise ValueError('Invalid process ID')
    if os.name!='nt':
        return linux_identity(pid)
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.OpenProcess(0x1000|0x00100000,False,pid)  # query + synchronize only
    if not handle:
        code=ctypes.get_last_error()
        if code==87:
            raise ProcessLookupError('Process does not exist')
        raise ctypes.WinError(code)
    try:
        return windows_identity(handle)
    finally:
        kernel.CloseHandle(handle)


def probe(expected):
    """Never return match for legacy PID-only records, foreign hosts or errors."""
    try:
        validate(expected)
    except (ValueError,TypeError,AttributeError,KeyError):
        return {'state':'unverified','reason':'invalid_or_legacy_identity'}
    platform='windows' if os.name=='nt' else 'linux' if sys.platform.startswith('linux') else 'unsupported'
    if expected['platform']!=platform:
        return {'state':'mismatch','reason':'platform_changed'}
    try:
        if expected['host_id']!=host_id():
            return {'state':'mismatch','reason':'host_changed'}
        observed=observe(expected['pid'])
    except (ProcessLookupError,FileNotFoundError):
        return {'state':'exited','reason':'process_exited'}
    except (OSError,ValueError,IndexError,KeyError,UnicodeError):
        return {'state':'unverified','reason':'identity_unavailable'}
    if observed!=expected:
        return {'state':'mismatch','reason':'birth_or_scope_changed'}
    return {'state':'match','reason':'birth_and_scope_match'}


def main():
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--observe',type=int)
    group.add_argument('--probe',action='store_true')
    args=parser.parse_args()
    if args.probe:
        data=sys.stdin.buffer.read(16385)
        if len(data)>16384:
            raise ValueError('Process probe request is too large')
        result=probe(json.loads(data))
    else:
        result=observe(args.observe)
    print(json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':
    main()
