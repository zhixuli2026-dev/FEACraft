"""Host private user inventory and DB attribution of legacy flat sessions."""
import asyncio
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import uuid

from .async_ops import finish_before_cancel
from .cli_private_paths import root
from .storage_usage import Counter,MeasurementUnavailable,_ordinary,_scan_fs_tree


def inventory(settings,deadline,counter=None):
    counter=counter if counter is not None else Counter(settings,deadline,None)
    initial=counter.entries;base=root(settings);parent=base/'sessions';rows={}
    try:
        _ordinary(base);info=_ordinary(parent)
    except FileNotFoundError:return rows,counter.entries-initial
    if not stat.S_ISDIR(info.st_mode):raise MeasurementUnavailable('Private CLI session inventory is invalid')
    with os.scandir(parent) as entries:
        for entry in entries:
            counter.add(0);info=_ordinary(Path(entry.path))
            if not stat.S_ISDIR(info.st_mode):raise MeasurementUnavailable('Private CLI session inventory is invalid')
            try:valid=str(uuid.UUID(entry.name))==entry.name
            except ValueError:valid=False
            if not valid:raise MeasurementUnavailable('Private CLI session inventory is invalid')
            rows[entry.name]=(info.st_dev,info.st_ino)
    counter.check()
    return rows,counter.entries-initial


async def bindings(settings,deadline):
    from sqlalchemy import select
    from ..db import SessionLocal
    from ..models import NativeCliSession
    observed,entries=await finish_before_cancel(asyncio.to_thread(inventory,settings,deadline))
    owners={}
    if observed:
        async with asyncio.timeout_at(deadline):
            async with SessionLocal() as db:
                identifiers=list(observed)
                for offset in range(0,len(identifiers),256):
                    result=await db.execute(select(NativeCliSession.id,NativeCliSession.created_by).where(
                        NativeCliSession.id.in_(identifiers[offset:offset+256])))
                    owners.update(result.all())
    return observed,owners,entries


def scan(settings,user_id,counter,binding):
    base=root(settings);selected=SimpleNamespace(storage_fs_root=str(base.parent))
    # Includes accounts, vaults and newly namespaced sessions, even before a
    # session's native owner row is committed. Never read private file contents.
    _scan_fs_tree(selected,(base.name,'users',str(user_id)),counter,link_bytes=True)
    observed,owners,_=binding
    current,_=inventory(settings,counter.deadline,counter)
    if current!=observed:raise MeasurementUnavailable('Private CLI session inventory changed')
    for identifier in sorted(current):
        counter.check();owner=owners.get(identifier)
        if owner is not None and (type(owner) is not int or owner<=0):
            raise MeasurementUnavailable('Private CLI session ownership is unavailable')
        if owner is not None and owner!=user_id:continue
        before=counter.bytes
        _scan_fs_tree(selected,(base.name,'sessions',identifier),counter,link_bytes=True)
        if owner is None and counter.bytes!=before:
            # Proven empty historical directories charge no bytes. Unknown
            # nonempty state cannot silently disappear from any user's quota.
            raise MeasurementUnavailable('Private CLI session ownership is unavailable')
    after,_=inventory(settings,counter.deadline,counter)
    if after!=current:raise MeasurementUnavailable('Private CLI session inventory changed')
    from .cli_native_usage import scan as native_scan
    native_scan(settings,counter,user_id=user_id)
