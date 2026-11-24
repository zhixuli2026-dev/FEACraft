"""Locate prepared replacements on the filesystem that will install them."""
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from ..storage_paths import FILE_STAGE
from . import file_transactions as files
from .owned_staging import batch


MOUNTED_SCHEMA = 'vibeflow.mounted-file-staging.v1'


@dataclass(frozen=True)
class Location:
    scratch: object
    device: int
    local: bool


def existing_parent(path):
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    return parent


def locate(root, relative, default, *, shared=None):
    parent = existing_parent(files._path(root, relative))
    device = parent.stat().st_dev
    if shared is not None and device == shared.device:
        return shared
    if device == existing_parent(default).stat().st_dev:
        return Location(default, device, False)
    while parent != root and parent.parent.stat().st_dev == device:
        parent = parent.parent
    return Location(files._path(parent, FILE_STAGE), device, True)


@asynccontextmanager
async def batches(objects, schema, *, default=None, tail=0):
    sizes = defaultdict(int)
    if default is not None:
        sizes[default] = tail
    for location, size in objects.values():
        sizes[location] += size
    async with AsyncExitStack() as stack:
        directories = {}
        for location, size in sizes.items():
            directories[location] = await stack.enter_async_context(batch(
                location.scratch, size, MOUNTED_SCHEMA if location.local else schema,
                target_root=location.scratch if location.local else None))
        yield directories
