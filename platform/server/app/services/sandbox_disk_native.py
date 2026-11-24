"""Bounded persistent sandbox image primitives for a trusted outer launch gate.

The caller must reserve node capacity before creation, retain the returned
lease outside payload user/PID namespaces, and prove all previous terminal and
export scopes stopped before reopening an image. This module does not grant
recovery authority, publish case files, or replay commands.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import selectors
import time
import uuid

IMAGE_BYTES = 128 * 1024 * 1024
SCHEMA = 'vibeflow.sandbox-disk.v1'
CLONE_NEWNS = 0x00020000


def ordinary(path, *, directory=False):
    info=path.lstat()
    if stat.S_ISLNK(info.st_mode) or (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) is not True:
        raise ValueError('Sandbox image path is not ordinary')
    if not directory and info.st_nlink!=1:
        raise ValueError('Sandbox image cannot have hard links')
    return info


def command(arguments):
    # Bound the pipe as it is consumed; never spool an unbounded utility log.
    # The outer gate runs this before its payload PID namespace exists. Reuse
    # its checked parent-death exec wrapper so mount/mkfs cannot outlive a killed
    # gate while a stop request is waiting in the gate's control queue.
    guarded=[sys.executable,'-I','-S','-u',str(Path(__file__).with_name('process_gate.py')),
             '--parent-pid',str(os.getpid()),'--',*arguments]
    process=subprocess.Popen(guarded,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    output=bytearray();deadline=time.monotonic()+30
    try:
        os.set_blocking(process.stdout.fileno(),False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Sandbox image utility exceeded its deadline')
                if not selector.select(remaining):continue
                data=os.read(process.stdout.fileno(),min(8192,65537-len(output)))
                if not data:break
                output.extend(data)
                if len(output)>65536:raise ValueError('Sandbox image utility output exceeds its bound')
        process.wait(timeout=max(.01,deadline-time.monotonic()))
        if process.returncode:raise OSError('Sandbox image utility failed: '+Path(arguments[0]).name)
        return bytes(output)
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
        process.stdout.close()


def private_mount_namespace():
    if sys.platform!='linux' or os.geteuid()!=0:
        raise ValueError('Persistent sandbox preparation requires the trusted outer Linux gate')
    before=os.readlink('/proc/self/ns/mnt')
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.unshare(CLONE_NEWNS)!=0:
        raise OSError(ctypes.get_errno(),'Cannot create private sandbox mount namespace')
    if os.readlink('/proc/self/ns/mnt')==before:
        raise ValueError('Sandbox mount namespace did not change')
    command(['mount','--make-rprivate','/'])


def root_path(value):
    root=Path(value)
    if not root.is_absolute() or root.resolve()!=root or not root.as_posix().startswith('/mnt/'):
        raise ValueError('Sandbox disk root must be an ordinary Windows-backed absolute path')
    for ancestor in (root,*root.parents):
        ordinary(ancestor,directory=True)
    if root.parent.name!='.terminal-export' or len(root.name)!=32 or uuid.UUID(root.name).hex!=root.name:
        raise ValueError('Sandbox disk is outside its private export root')
    return root


def inspect_image(path,descriptor,expected=None):
    before=ordinary(path)
    actual=os.fstat(descriptor)
    if (before.st_dev,before.st_ino,before.st_size)!=(actual.st_dev,actual.st_ino,actual.st_size):
        raise ValueError('Sandbox disk path changed while opening')
    if actual.st_size!=IMAGE_BYTES:
        raise ValueError('Sandbox disk image has no fixed admitted size')
    observed={'device':actual.st_dev,'inode':actual.st_ino,'bytes':actual.st_size}
    if expected is not None and observed!=expected:
        raise ValueError('Sandbox disk identity differs from its receipt')
    return observed


class Image:
    """One private mount and an exclusive advisory lease, held until drained.

    The lease is an additional exclusion mechanism, not OS process exit proof.
    A previous exporter may still hold an open directory after its parent dies.
    """
    def __init__(self,root,descriptor,manifest):
        self.root=root;self.fd=descriptor;self.manifest=manifest
        self.path=root/'volume.ext4';self.mount=root/'disk';self.mounted=False

    @classmethod
    def create(cls,root):
        root=root_path(root)
        path=root/'volume.ext4'
        descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            os.ftruncate(descriptor,IMAGE_BYTES);os.fsync(descriptor)
            binding=inspect_image(path,descriptor)
            identifier=str(uuid.uuid4())
            command(['mkfs.ext4','-q','-F','-m','0','-U',identifier,
                     '-E','lazy_itable_init=0,lazy_journal_init=0',str(path)])
            inspect_image(path,descriptor,binding)
            manifest={'schema':SCHEMA,'uuid':identifier,'image':binding,'filesystem':'ext4'}
            result=cls(root,descriptor,manifest)
            result.mount.mkdir(exist_ok=False)
            with (root/'disk.json').open('xb') as output:
                output.write(json.dumps(manifest,sort_keys=True).encode());output.flush();os.fsync(output.fileno())
            return result
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def reopen_readonly(cls,root,manifest):
        """Caller must first verify durable owners and all recorded scope exits."""
        root=root_path(root)
        if (not isinstance(manifest,dict) or set(manifest)!={'schema','uuid','image','filesystem'}
                or manifest['schema']!=SCHEMA or manifest['filesystem']!='ext4'
                or str(uuid.UUID(manifest['uuid']))!=manifest['uuid']):
            raise ValueError('Invalid sandbox disk receipt')
        descriptor=os.open(root/'volume.ext4',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            inspect_image(root/'volume.ext4',descriptor,manifest['image'])
            ordinary(root/'disk',directory=True)
            return cls(root,descriptor,manifest)
        except BaseException:
            os.close(descriptor)
            raise

    def attach(self,*,readonly=False):
        if self.fd is None or self.mounted:
            raise ValueError('Sandbox image is not awaiting a private mount')
        inspect_image(self.path,self.fd,self.manifest['image'])
        actual=command(['blkid','-p','-s','UUID','-o','value',str(self.path)]).decode('ascii').strip()
        if actual!=self.manifest['uuid']:
            raise ValueError('Sandbox filesystem UUID differs from its receipt')
        # Do not attach an already mounted image under a second loop device.
        # The process owner still must prove old scopes stopped; this check is
        # not sufficient authority to recover or detach any existing device.
        if command(['losetup','-j',str(self.path)]).strip():
            raise ValueError('Sandbox image still has a live loop attachment')
        private_mount_namespace()
        options='loop,ro,noload,nodev,nosuid,noatime' if readonly else 'loop,sync,dirsync,nodev,nosuid,noatime'
        command(['mount','-t','ext4','-o',options,str(self.path),str(self.mount)])
        self.mounted=True
        size=os.statvfs(self.mount)
        if not 0<size.f_frsize*size.f_blocks<=IMAGE_BYTES:
            raise ValueError('Mounted sandbox filesystem exceeds its image bound')
        return self.mount

    def close(self):
        if self.mounted:
            # Only detach this caller's private mount. Retained exporter FDs
            # continue to pin their superblock; their owners need separate exit
            # proof before a recovery operation may reopen the image.
            command(['umount','-l',str(self.mount)])
            self.mounted=False
        if self.fd is not None:
            os.close(self.fd);self.fd=None

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
