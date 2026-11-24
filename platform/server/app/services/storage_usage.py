"""Bounded observation of retained user storage, outside the event loop.

This is a measured inventory, not a concurrent write reservation or disk quota.
S3 uses physical keys/versions, never the logical file-tree projection.
"""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import time
from weakref import WeakKeyDictionary

from ..config import get_settings
from .async_ops import finish_before_cancel

_slots=WeakKeyDictionary()


class MeasurementUnavailable(ValueError):pass
class _Enough(Exception):pass


@dataclass(frozen=True)
class Usage:
    backend: str
    bytes: int
    entries: int
    complete: bool


class Counter:
    def __init__(self,settings,deadline,stop_after):
        self.deadline=deadline;self.maximum=settings.storage_measurement_max_entries
        self.stop_after=stop_after;self.bytes=0;self.entries=0

    def check(self):
        if time.monotonic()>=self.deadline:raise MeasurementUnavailable('Storage measurement exceeded its deadline')

    def add(self,size):
        self.check()
        if type(size) is not int or size<0:raise MeasurementUnavailable('Invalid stored size')
        self.entries+=1
        if self.entries>self.maximum:raise MeasurementUnavailable('Storage measurement entry limit exceeded')
        self.bytes+=size
        if self.stop_after is not None and self.bytes>self.stop_after:raise _Enough()


def _ordinary(path):
    return _ordinary_info(path.lstat())


def _ordinary_info(info):
    """Validate the same no-follow observation used by the caller."""
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
        raise MeasurementUnavailable('Storage measurement cannot follow links or reparse points')
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise MeasurementUnavailable('Storage measurement encountered a special file')
    return info


def _scan_fs(settings,user_id,counter):
    from .trash import _mutation_lock
    # Count both physical locations while cooperating delete/restore cannot
    # move a tree between an already-scanned root and a not-yet-scanned root.
    # The shared OS lock also covers peers and failed index-write rollbacks.
    root=Path(settings.storage_fs_root).resolve()
    try:
        info=_ordinary(root/'_trash')
        if not stat.S_ISDIR(info.st_mode):raise MeasurementUnavailable('Trash root is not a directory')
    except FileNotFoundError:pass
    counter.check()
    with _mutation_lock(deadline=counter.deadline):
        _scan_fs_tree(settings,('workspaces',str(user_id)),counter)
        _scan_fs_tree(settings,('_trash','users',str(user_id)),counter)


def _scan_fs_tree(settings,parts,counter,*,link_bytes=False):
    # An owned native writer can atomically replace a temporary file between
    # scandir and lstat. Restart this whole tree instead of skipping the missing
    # entry or treating a partial inventory as complete. Keep the original
    # deadline and cumulative entry budget across all three attempts.
    initial_bytes=counter.bytes
    for attempt in range(3):
        try:return _scan_fs_tree_once(settings,parts,counter,link_bytes=link_bytes)
        except FileNotFoundError as error:
            counter.bytes=initial_bytes
            counter.check()
            if attempt==2:raise MeasurementUnavailable('Storage inventory kept changing during bounded observation') from error


def _scan_fs_tree_once(settings,parts,counter,*,link_bytes=False):
    # The configured root may resolve an operator-selected mount. User paths
    # below it must not redirect the inventory into another user's files.
    counter.check()
    current=Path(settings.storage_fs_root).resolve()
    for part in parts:
        counter.check();current=current/part
        try:info=_ordinary(current)
        except FileNotFoundError:return
        if not stat.S_ISDIR(info.st_mode):raise MeasurementUnavailable('User storage root is not a directory')
    root_identity=(info.st_dev,info.st_ino)
    stack=[(current,root_identity)]
    while stack:
        directory,expected=stack.pop();counter.check()
        observed=_ordinary(directory)
        if (observed.st_dev,observed.st_ino)!=expected:raise MeasurementUnavailable('Storage directory changed')
        counter.check()
        with os.scandir(directory) as entries:
            while True:
                counter.check()
                entry=next(entries,None)
                if entry is None:
                    break
                counter.check()
                info=Path(entry.path).lstat()
                # Linux CLI caches contain executable symlinks. Account for
                # the link itself as the native WSL inventory does; never
                # traverse its target. Public case trees retain strict checks.
                if link_bytes and os.name!='nt' and stat.S_ISLNK(info.st_mode):
                    counter.add(info.st_size)
                    continue
                info=_ordinary_info(info)
                counter.add(info.st_size if stat.S_ISREG(info.st_mode) else 0)
                if stat.S_ISDIR(info.st_mode):stack.append((Path(entry.path),(info.st_dev,info.st_ino)))
        counter.check()
        observed=_ordinary(directory)
        if (observed.st_dev,observed.st_ino)!=expected:raise MeasurementUnavailable('Storage directory changed')
    counter.check()
    observed=_ordinary(current)
    if (observed.st_dev,observed.st_ino)!=root_identity:raise MeasurementUnavailable('User storage root changed')


def _raw_client(settings,deadline):
    import boto3
    from botocore.config import Config
    remaining=max(.05,deadline-time.monotonic())
    return boto3.client('s3',endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,aws_secret_access_key=settings.s3_secret_key,
        region_name='us-east-1',config=Config(signature_version='s3v4',s3={'addressing_style':'path'},
            connect_timeout=min(3,remaining),read_timeout=min(5,remaining),retries={'max_attempts':0}))


def _scan_s3(settings,user_id,counter):
    prefix=f'workspaces/{user_id}/'
    counter.check();client=_raw_client(settings,counter.deadline)
    try:
        versioning=client.get_bucket_versioning(Bucket=settings.s3_bucket).get('Status')
        if versioning not in {None,'Enabled','Suspended'}:
            raise MeasurementUnavailable('Unknown bucket versioning state')
        versioned=versioning in {'Enabled','Suspended'}
        operation='list_object_versions' if versioned else 'list_objects_v2'
        counter.check()
        for page in client.get_paginator(operation).paginate(Bucket=settings.s3_bucket,Prefix=prefix,
                PaginationConfig={'PageSize':settings.storage_measurement_page_size}):
            counter.check()
            for item in page.get('Versions' if versioned else 'Contents',[]):
                if not isinstance(item.get('Key'),str) or not item['Key'].startswith(prefix):
                    raise MeasurementUnavailable('Object inventory crossed user ownership')
                counter.add(item['Size'])
            for marker in page.get('DeleteMarkers',[]):
                if not isinstance(marker.get('Key'),str) or not marker['Key'].startswith(prefix):
                    raise MeasurementUnavailable('Version marker crossed user ownership')
                counter.add(0)
    finally:client.close()


def _measure(settings,user_id,deadline,stop_after,private_binding=None):
    counter=Counter(settings,deadline,stop_after);complete=True
    try:
        from . import cli_private_usage
        if private_binding is None:
            observed,entries=cli_private_usage.inventory(settings,deadline)
            private_binding=(observed,{},entries)
        counter.entries=private_binding[2]
        if settings.storage_backend=='fs':_scan_fs(settings,user_id,counter)
        elif settings.storage_backend=='s3':_scan_s3(settings,user_id,counter)
        else:raise MeasurementUnavailable('Unknown storage backend')
        from .skill_capacity import scan as skill_scan
        skill_scan(settings,counter,user_id=user_id)
        cli_private_usage.scan(settings,user_id,counter,private_binding)
        counter.check()
    except _Enough:complete=False  # Proven lower bound already exceeds admission allowance.
    except MeasurementUnavailable:raise
    except Exception as error:
        raise MeasurementUnavailable('Storage inventory could not be verified') from error
    return Usage(settings.storage_backend,counter.bytes,counter.entries,complete)


async def measure_user(user_id,*,stop_after=None):
    if type(user_id) is not int or user_id<=0:raise ValueError('Invalid storage owner')
    if stop_after is not None and (type(stop_after) is not int or stop_after<0):
        raise ValueError('Invalid storage allowance')
    settings=get_settings();loop=asyncio.get_running_loop()
    slots=_slots.setdefault(loop,asyncio.Semaphore(settings.storage_measurement_concurrency))
    from .request_deadline import current
    parent=current()
    if parent is not None:parent.check()
    deadline=time.monotonic()+settings.storage_measurement_timeout_seconds
    if parent is not None:deadline=min(deadline,parent.deadline)
    try:await asyncio.wait_for(slots.acquire(),timeout=max(0,deadline-time.monotonic()))
    except asyncio.TimeoutError as error:raise MeasurementUnavailable('Storage inventory queue timed out') from error
    try:
        from .cli_private_usage import bindings
        try:private_binding=await bindings(settings,deadline)
        except asyncio.CancelledError:raise
        except Exception as error:
            raise MeasurementUnavailable('Private CLI ownership inventory could not be verified') from error
        # Cancellation must not return the slot while its I/O thread is alive.
        return await finish_before_cancel(asyncio.to_thread(_measure,settings,user_id,deadline,stop_after,private_binding))
    finally:slots.release()
