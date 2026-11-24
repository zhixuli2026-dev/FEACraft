"""Cross-process admission for cooperating local cache allocations.

Claims cover the whole allocation until its I/O drains. Current files plus full
live claims are conservative (materialized portions can be counted twice).
This is not a filesystem quota against native or external writers.
"""
from contextlib import contextmanager,asynccontextmanager
import asyncio
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from types import SimpleNamespace
import uuid
import json

from .. import storage
from ..config import get_settings
from . import process_identity_native as identity
from .case_write import CaseWriteConflict
from .durable_events import json_text
from .storage_usage import Counter, MeasurementUnavailable, _ordinary, _scan_fs_tree

SCHEMA='vibeflow.node-storage-claim.v1'
DIRECTORY='.node-storage-budget'
_LOCK=threading.Lock()


def unavailable():
    return CaseWriteConflict('node_storage_unavailable','节点存储用量无法确认，请稍后重试')


@contextmanager
def _locked(root,deadline):
    from .file_transactions import _path
    if not _LOCK.acquire(timeout=max(0,deadline-time.monotonic())):raise unavailable()
    try:
        parent=_path(root,DIRECTORY);parent.mkdir(exist_ok=True)
        path=_path(root,DIRECTORY+'/lock')
        with path.open('a+b') as stream:
            stream.seek(0,os.SEEK_END)
            if stream.tell()==0:stream.write(b'\0');stream.flush()
            acquired=False
            try:
                while not acquired:
                    if time.monotonic()>=deadline:raise unavailable()
                    stream.seek(0)
                    try:
                        if os.name=='nt':
                            import msvcrt
                            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                        acquired=True
                    except (BlockingIOError,PermissionError):time.sleep(.02)
                yield parent
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                    else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
    finally:_LOCK.release()


def _claims(root,parent,counter):
    reserved=0
    for path in parent.iterdir():
        counter.check()
        if path.name=='lock':continue
        info=_ordinary(path);counter.add(0)
        from .cli_node_capacity import fragment
        piece=fragment(path,info)
        if piece is not None:
            reserved+=piece['reserved_bytes']
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>16384
                or not re.fullmatch('[0-9a-f]{32}\\.json',path.name)):
            raise unavailable()
        row=json.loads(path.read_bytes())
        from .cli_node_capacity import SCHEMA as native_schema,validate as validate_native
        if row.get('schema')==native_schema:
            validate_native(row,root,path.stem)
            # The API's death cannot prove that its native initializer stopped.
            # Only the durable CLI prepared/stop reconciliation removes this.
            reserved+=row['bytes']
            continue
        if (row.get('schema')!=SCHEMA or row.get('id')!=path.stem or row.get('root')!=str(root)
                or type(row.get('bytes')) is not int or row['bytes']<0):raise unavailable()
        identity.validate(row['owner'])
        if identity.probe(row['owner'])['state']=='exited':
            # Files survive process death and remain in the physical inventory.
            path.unlink()
        else:reserved+=row['bytes']
    return reserved


def _admit(size,*,private=False,owner=None,target_root=None):
    if type(size) is not int or size<0:raise ValueError('Invalid node allocation size')
    if type(private) is not bool:raise ValueError('Invalid private allocation selection')
    from .request_deadline import current
    request_budget=current()
    if request_budget is not None:request_budget.check()
    from .file_transactions import _write_new
    settings=get_settings();root=storage._fs_root().resolve();root.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+settings.storage_measurement_timeout_seconds
    if request_budget is not None:deadline=min(deadline,request_budget.deadline)
    counter=Counter(settings,deadline,None)
    identifier=uuid.uuid4().hex
    holder=identity.observe(os.getpid()) if owner is None else identity.validate(owner)
    if holder['host_id']!=identity.host_id() or identity.probe(holder)['state']!='match':
        raise unavailable()
    row={'schema':SCHEMA,'id':identifier,'root':str(root),'owner':holder,'bytes':size}
    target=Path(target_root).resolve() if target_root is not None else None
    if target is not None:row['target_root']=str(target)
    encoded=json_text(row).encode('utf8')
    if len(encoded)>16384:raise unavailable()
    with _locked(root,deadline) as parent:
        _capacity(settings,root,parent,counter,size+len(encoded),private=private,target_root=target)
        path=parent/(identifier+'.json');_write_new(path,encoded)
    return root,path,encoded


def _capacity(settings,root,parent,counter,extra,*,private,target_root=None):
    counter.phase='claims'
    reserved=_claims(root,parent,counter)
    from .cli_private_paths import root as private_root,volume_path
    private_path=private_root(settings)
    native_path=None
    # One inventory for admission and runtime observation, including a native
    # root only when explicitly configured. This is not a filesystem quota.
    for retry in range(3):
        counter.bytes=0
        try:
            counter.phase='public_inventory'
            _scan_fs_tree(SimpleNamespace(storage_fs_root=str(root.parent)),(root.name,),counter)
            counter.phase='private_inventory'
            _scan_fs_tree(SimpleNamespace(storage_fs_root=str(private_path.parent)),(private_path.name,),counter,link_bytes=True)
            counter.phase='skill_inventory'
            from .skill_capacity import scan as skill_scan
            skill_scan(settings,counter)
            counter.phase='native_private_inventory'
            from .cli_native_usage import scan as native_scan
            native_path=native_scan(settings,counter)
            break
        except FileNotFoundError:
            # Native tools rotate temporary files. Restart the entire inventory
            # without resetting the shared deadline or cumulative entry bound.
            if retry==2:raise
            counter.check()
    needed=reserved+extra
    counter.phase='free_space_and_limits'
    if (counter.bytes+needed>settings.node_storage_limit_mb*1024*1024 or
            shutil.disk_usage(root).free<needed+settings.node_storage_min_free_mb*1024*1024 or
            (private and shutil.disk_usage(volume_path(private_path)).free<needed+settings.node_storage_min_free_mb*1024*1024) or
            (private and native_path is not None and shutil.disk_usage(native_path).free<needed+settings.node_storage_min_free_mb*1024*1024) or
            (target_root is not None and shutil.disk_usage(volume_path(target_root)).free<needed+settings.node_storage_min_free_mb*1024*1024)):
        raise CaseWriteConflict('node_storage_full','节点缓存或磁盘剩余空间不足，请清理已确认可删除的缓存或扩容后重试')
    counter.check()
    return {'bytes':counter.bytes,'reserved_bytes':reserved,
        'scope':'host_and_configured_native_cli' if native_path is not None else 'host_public_and_private_cli'}


def observe(*,private=False):
    """Bounded runtime observation, independent of the expired startup request.

    Uses the same OS lock and inventory as admission without creating a claim.
    The caller must drain this synchronous operation when cancelled.
    """
    if type(private) is not bool:raise ValueError('Invalid private observation selection')
    started=time.monotonic();counter=None
    try:
        settings=get_settings();root=storage._fs_root().resolve();root.mkdir(parents=True,exist_ok=True)
        deadline=time.monotonic()+settings.storage_measurement_timeout_seconds
        counter=Counter(settings,deadline,None)
        counter.phase='lock'
        with _locked(root,deadline) as parent:
            return _capacity(settings,root,parent,counter,0,private=private)
    except (OSError,ValueError,KeyError,TypeError,MeasurementUnavailable) as error:
        failure=error if isinstance(error,CaseWriteConflict) else unavailable()
        reasons={'Storage measurement exceeded its deadline':'deadline',
            'Storage measurement entry limit exceeded':'entry_limit',
            'Storage measurement cannot follow links or reparse points':'link',
            'Storage measurement encountered a special file':'special_file',
            'Storage directory changed':'directory_changed','User storage root changed':'root_changed',
            'Native CLI state root is unavailable':'native_unavailable','Native CLI state root is redirected':'native_redirected',
            'Native CLI state root changed':'native_changed'}
        failure.storage_observation={'phase':getattr(counter,'phase','setup'),
            'elapsed_seconds':round(time.monotonic()-started,3),'entries':getattr(counter,'entries',0),
            'bytes':getattr(counter,'bytes',0),'cause':type(error).__name__,
            'errno':getattr(error,'errno',None),'reason':reasons.get(str(error),'unclassified')}
        if hasattr(error,'native_entry_metadata'):
            failure.storage_observation['native_entry_metadata']=error.native_entry_metadata
        if failure is error:raise
        raise failure from error


def _release(root,path,encoded):
    deadline=time.monotonic()+get_settings().storage_measurement_timeout_seconds
    with _locked(root,deadline):
        info=_ordinary(path)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or path.read_bytes()!=encoded:raise unavailable()
        path.unlink()


@contextmanager
def reserve(size,*,private=False,target_root=None):
    """Synchronous I/O scope: callers must drain this scope before cancellation."""
    try:
        if target_root is not None:claim=_admit(size,private=private,target_root=target_root)
        else:claim=_admit(size,private=True) if private else _admit(size)
    except CaseWriteConflict:raise
    except (OSError,ValueError,KeyError,TypeError,MeasurementUnavailable) as error:
        raise unavailable() from error
    try:yield
    finally:_release(*claim)


@asynccontextmanager
async def allocation(size, *, private=False, target_root=None):
    """Hold a node claim across several drained async/thread I/O operations."""
    from .async_ops import finish_before_cancel
    if type(private) is not bool:
        raise ValueError('Invalid private allocation selection')
    options = {}
    if private:
        options['private'] = True
    if target_root is not None:
        options['target_root'] = target_root
    claim=None
    async def acquire():
        nonlocal claim
        try:
            claim=await asyncio.to_thread(_admit,size,**options)
        except CaseWriteConflict:raise
        except (OSError,ValueError,KeyError,TypeError,MeasurementUnavailable) as error:
            raise unavailable() from error
    try:
        # Save a committed claim even when cancellation arrives during admission.
        await finish_before_cancel(acquire())
        yield
    finally:
        if claim is not None:
            await finish_before_cancel(asyncio.to_thread(_release,*claim))


def copy_bytes(records,encoded,copies=1):
    sizes=[row['size'] for row in records.values()]
    if any(type(size) is not int or size<0 for size in sizes):raise ValueError('Invalid node copy inventory')
    return sum(sizes)*copies+len(encoded)
