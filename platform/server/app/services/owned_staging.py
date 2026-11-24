"""Node-admitted private scratch with a process-bound cleanup marker."""
from contextlib import asynccontextmanager
from functools import partial
from itertools import islice
import json
import os
import re
import uuid

from . import case_provision, node_storage_budget, process_identity_native
from .attempt_workspace import ordinary
from .file_transactions import _write_new
from .fs_directory import remove_owned


def mark_owner(root, directory, schema):
    guard = {'schema': schema, 'root': str(root), 'id': directory.name,
             'process': process_identity_native.observe(os.getpid())}
    _write_new(directory / 'owner.json', json.dumps(guard).encode('utf8'))


def recover_abandoned(root, schema):
    if not root.exists():
        return
    ordinary(root)
    for path in islice(root.iterdir(), 32):
        case_provision.check_deadline()
        if not re.fullmatch('[0-9a-f]{32}', path.name):
            continue
        try:
            ordinary(path)
            marker = path / 'owner.json'
            if ordinary(marker).st_size > 4096:
                continue
            with marker.open('rb') as stream:
                guard = json.loads(stream.read(4097))
            birth = guard['process']
            if guard != {'schema': schema, 'root': str(root), 'id': path.name, 'process': birth}:
                continue
            if process_identity_native.probe(birth)['state'] == 'exited':
                remove_owned(path, root=root)
        except (OSError, ValueError, KeyError, TypeError):
            continue


@asynccontextmanager
async def batch(root, size, schema, *, target_root=None):
    directory = root / uuid.uuid4().hex
    await case_provision.io(recover_abandoned, root, schema)
    allocation = (node_storage_budget.allocation(size + 4096) if target_root is None else
                  node_storage_budget.allocation(size + 4096, target_root=target_root))
    async with allocation:
        created = False

        def create():
            nonlocal created
            case_provision.check_deadline()
            directory.mkdir(parents=True, exist_ok=False)
            created = True
            mark_owner(root, directory, schema)

        try:
            await case_provision.io(create)
            yield directory
        finally:
            if created:
                await case_provision.io(partial(remove_owned, directory, root=root))
