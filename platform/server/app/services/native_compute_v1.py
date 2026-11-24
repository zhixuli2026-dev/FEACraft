"""Compute controls for WSL kernels that expose CPU and memory through cgroup v1."""

from contextlib import ExitStack, contextmanager
import ctypes
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


SCHEMA = 'vibeflow.native-compute-v1.v1'
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
_FILE_FLAGS = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
_PAGE_SIZE = 4096
_CPU_PERIOD = 10000


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != {'cpu_limit_ns', 'memory_limit_bytes'}:
        raise ValueError('Invalid native compute policy')
    cpu = value['cpu_limit_ns']
    memory = value['memory_limit_bytes']
    if (type(cpu) is not int or not 0 <= cpu <= 86_400_000_000_000
            or type(memory) is not int or not 0 <= memory < 2**63 or memory % _PAGE_SIZE):
        raise ValueError('Native compute limits must be bounded CPU nanoseconds and page-aligned memory bytes')
    return value


def _controllers(policy):
    return (('cpu','cpuacct') if policy['cpu_limit_ns'] else ()) + (
        ('memory',) if policy['memory_limit_bytes'] else ())


def _name(nonce):
    if not isinstance(nonce,str) or re.fullmatch('[0-9a-f]{64}',nonce) is None:
        raise ValueError('Invalid native compute scope nonce')
    return 'vibeflow-compute-'+nonce


def validate(descriptor):
    if (not isinstance(descriptor,dict) or set(descriptor)!={'schema','nonce','policy','controllers'}
            or descriptor['schema']!=SCHEMA):
        raise ValueError('Invalid native compute scope')
    _name(descriptor['nonce'])
    controllers=descriptor['controllers']
    expected=_controllers(validate_policy(descriptor['policy']))
    if not expected or not isinstance(controllers,dict) or set(controllers)!=set(expected):
        raise ValueError('Unexpected native compute controllers')
    for record in controllers.values():
        if (not isinstance(record,dict) or set(record)!={'device','inode'}
                or any(type(value) is not int or not 0<value<2**64 for value in record.values())):
            raise ValueError('Invalid native compute cgroup identity')
    return descriptor


def _identity(fd):
    info=os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
        raise ValueError('Native compute cgroups must be owned by root')
    kernel=ctypes.CDLL(None,use_errno=True)
    kernel.fstatfs.argtypes=[ctypes.c_int,ctypes.c_void_p]
    kernel.fstatfs.restype=ctypes.c_int
    filesystem=ctypes.create_string_buffer(512)
    if kernel.fstatfs(fd,filesystem)!=0:
        raise OSError(ctypes.get_errno(),'Could not verify native compute controller filesystem')
    if ctypes.c_long.from_buffer(filesystem).value!=0x27E0EB:
        raise ValueError('Expected a cgroup v1 controller filesystem')
    return {'device':info.st_dev,'inode':info.st_ino}


@contextmanager
def _mounts(controllers, *, retain_files=False):
    if not sys.platform.startswith('linux') or os.geteuid()!=0:
        raise OSError('Native compute scope requires the trusted Linux launch helper')
    kernel=ctypes.CDLL(None,use_errno=True)
    kernel.unshare.argtypes=[ctypes.c_int]
    kernel.unshare.restype=ctypes.c_int
    kernel.mount.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_ulong,ctypes.c_char_p]
    kernel.mount.restype=ctypes.c_int
    kernel.umount2.argtypes=[ctypes.c_char_p,ctypes.c_int]
    kernel.umount2.restype=ctypes.c_int
    if kernel.unshare(0x00020000)!=0:  # CLONE_NEWNS
        raise OSError(ctypes.get_errno(),'Could not create a private native compute mount namespace')
    if kernel.mount(None,b'/',None,0x44000,None)!=0:  # MS_REC | MS_PRIVATE
        raise OSError(ctypes.get_errno(),'Could not isolate native compute mount propagation')

    def unmount(path):
        if kernel.umount2(path,2 if retain_files else 0)!=0:  # MNT_DETACH keeps open read handles valid.
            raise OSError(ctypes.get_errno(),'Could not release a private native compute mount')

    with tempfile.TemporaryDirectory(prefix='vibeflow-compute-',dir='/tmp') as private, ExitStack() as stack:
        result={}
        for controller in controllers:
            path=Path(private)/controller
            path.mkdir()
            mount_path=os.fsencode(path)
            if kernel.mount(b'none',mount_path,b'cgroup',14,controller.encode('ascii'))!=0:  # NOSUID | NODEV | NOEXEC
                raise OSError(ctypes.get_errno(),'Could not mount the native '+controller+' controller')
            stack.callback(unmount,mount_path)
            fd=os.open(path,_DIRECTORY_FLAGS)
            stack.callback(os.close,fd)
            _identity(fd)
            result[controller]=fd
        yield result


def _read(fd, filename):
    member=os.open(filename,os.O_RDONLY|_FILE_FLAGS,dir_fd=fd)
    try:
        data=os.read(member,4097)
        if len(data)>4096:
            raise ValueError('Native compute record exceeds its size limit')
        return data.decode('ascii').strip()
    finally:
        os.close(member)


def _write(fd, filename, number):
    member=os.open(filename,os.O_WRONLY|_FILE_FLAGS,dir_fd=fd)
    try:
        data=str(number).encode('ascii')
        if os.write(member,data)!=len(data):
            raise OSError('Incomplete native compute control write')
    finally:
        os.close(member)


def _limits(groups, policy):
    if 'cpu' in groups:
        cpu=groups['cpu']
        _write(cpu,'cpu.cfs_period_us',_CPU_PERIOD)
        _write(cpu,'cpu.cfs_quota_us',_CPU_PERIOD)
        if any(int(_read(cpu,name))!=_CPU_PERIOD for name in ('cpu.cfs_period_us','cpu.cfs_quota_us')):
            raise ValueError('The native CPU bandwidth limit was not applied')
    if 'memory' in groups:
        memory=groups['memory']
        limit=policy['memory_limit_bytes']
        for name in ('memory.limit_in_bytes','memory.memsw.limit_in_bytes'):
            _write(memory,name,limit)
            if int(_read(memory,name))!=limit:
                raise ValueError('The native memory limit was not applied: '+name)


def allocate(nonce, policy):
    selected=_name(nonce)
    policy=validate_policy(policy)
    controllers=_controllers(policy)
    if not controllers:
        return None
    with _mounts(controllers) as mounts, ExitStack() as stack:
        prepared={}
        groups={}
        try:
            for controller,parent in mounts.items():
                os.mkdir(selected,mode=0o755,dir_fd=parent)
                prepared[controller]=parent
                group=os.open(selected,_DIRECTORY_FLAGS,dir_fd=parent)
                stack.callback(os.close,group)
                groups[controller]=group
            _limits(groups,policy)
            return validate({'schema':SCHEMA,'nonce':nonce,'policy':policy,
                'controllers':{name:_identity(fd) for name,fd in groups.items()}})
        except BaseException:
            for parent in prepared.values():
                os.rmdir(selected,dir_fd=parent)
            raise


@contextmanager
def _bound(descriptor, *, allow_absent=False, retain_files=False):
    validate(descriptor)
    selected=_name(descriptor['nonce'])
    with _mounts(descriptor['controllers'],retain_files=retain_files) as mounts, ExitStack() as stack:
        groups={}
        for controller,parent in mounts.items():
            try:
                group=os.open(selected,_DIRECTORY_FLAGS,dir_fd=parent)
            except FileNotFoundError:
                if allow_absent:
                    continue
                raise
            stack.callback(os.close,group)
            if _identity(group)!=descriptor['controllers'][controller]:
                raise ValueError('Native compute scope identity changed')
            groups[controller]=group
        yield selected,mounts,groups


class RuntimeMonitor:
    def __init__(self, files, policy):
        self.files=files
        self.policy=policy

    def violation(self):
        if 'memory' in self.files:
            data=os.pread(self.files['memory'],4096,0).decode('ascii')
            events=dict(line.split() for line in data.splitlines())
            if int(events['oom_kill'])>0:
                return 'memory_limit'
        if 'cpuacct' in self.files:
            used=int(os.pread(self.files['cpuacct'],128,0))
            if used>=self.policy['cpu_limit_ns']:
                return 'cpu_limit'
        return None

    def close(self):
        for fd in self.files.values():
            os.close(fd)


def join(descriptor, *, monitor=False):
    files={}
    try:
        with _bound(descriptor,retain_files=monitor) as (selected,_,groups):
            for fd in groups.values():
                _write(fd,'cgroup.procs',0)
            if monitor:
                for name,filename in (('cpuacct','cpuacct.usage'),('memory','memory.oom_control')):
                    if name in groups:
                        files[name]=os.open(filename,os.O_RDONLY|_FILE_FLAGS,dir_fd=groups[name])
        membership={controller:path for line in Path('/proc/self/cgroup').read_text(encoding='ascii').splitlines()
                    for _,controllers,path in (line.split(':',2),) for controller in controllers.split(',')}
        if any(membership.get(controller)!='/'+selected for controller in descriptor['controllers']):
            raise ValueError('The native gate did not enter all of its compute cgroups')
    except BaseException:
        for fd in files.values():
            os.close(fd)
        raise
    return RuntimeMonitor(files,descriptor['policy']) if monitor else None


def _usage(groups):
    result={}
    if 'cpuacct' in groups:
        result['cpu_used_ns']=int(_read(groups['cpuacct'],'cpuacct.usage'))
    if 'memory' in groups:
        memory=groups['memory']
        events=dict(line.split() for line in _read(memory,'memory.oom_control').splitlines())
        result.update(memory_peak_bytes=int(_read(memory,'memory.max_usage_in_bytes')),
            memory_and_swap_peak_bytes=int(_read(memory,'memory.memsw.max_usage_in_bytes')),
            memory_oom_kills=int(events['oom_kill']))
    return result


def validate_usage(descriptor, value, *, allow_absent=False):
    validate(descriptor)
    fields=set()
    if 'cpuacct' in descriptor['controllers']:
        fields.add('cpu_used_ns')
    if 'memory' in descriptor['controllers']:
        fields.update({'memory_peak_bytes','memory_and_swap_peak_bytes','memory_oom_kills'})
    if (not isinstance(value,dict) or not value.keys()<=fields or (not allow_absent and value.keys()!=fields)
            or any(type(item) is not int or not 0<=item<2**64 for item in value.values())):
        raise ValueError('Invalid native compute observation')
    return value


def observe(descriptor):
    with _bound(descriptor) as (_,_,groups):
        return validate_usage(descriptor,_usage(groups))


def retire(descriptor):
    with _bound(descriptor,allow_absent=True) as (selected,mounts,groups):
        if any(_read(fd,'cgroup.procs') for fd in groups.values()):
            raise ValueError('Native compute scopes still contain processes')
        result=_usage(groups)
        for controller in groups:
            os.rmdir(selected,dir_fd=mounts[controller])
        return validate_usage(descriptor,result,allow_absent=True)


def discard_unadmitted(nonce, policy):
    """Revoke groups of an exited, durably permitted allocator before any launch."""
    selected=_name(nonce)
    with _mounts(_controllers(validate_policy(policy))) as mounts:
        for parent in mounts.values():
            try:
                fd=os.open(selected,_DIRECTORY_FLAGS,dir_fd=parent)
            except FileNotFoundError:
                continue
            try:
                original=_identity(fd)
                current=os.stat(selected,dir_fd=parent,follow_symlinks=False)
                if _read(fd,'cgroup.procs') or (current.st_dev,current.st_ino)!=(original['device'],original['inode']):
                    raise ValueError('An unadmitted compute scope is not empty or has changed')
                os.rmdir(selected,dir_fd=parent)
            finally:
                os.close(fd)
