"""Provision a single native Linux home in app-managed private storage."""

import os
from pathlib import Path
import shutil

from .case_write import CaseWriteConflict
from .cli_private_paths import root as cli_state_root
from .terminal_protocol import validate_linux_home


def select(settings, user_id):
    if not settings.linux_shell_state_root:
        return None
    if type(user_id) is not int or user_id <= 0:
        raise ValueError('Linux shell home requires a registered account')
    root = Path(settings.linux_shell_state_root)
    public = Path(settings.storage_fs_root).resolve()
    cli = cli_state_root(settings)
    if (not root.is_absolute() or root != root.resolve() or root == Path('/')
            or any(root.is_relative_to(folder) or folder.is_relative_to(root) for folder in (public, cli))):
        raise ValueError('Linux shell homes require a separate private volume path')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < settings.node_storage_min_free_mb * 1024**2:
        raise CaseWriteConflict('node_storage_full', 'Linux shell home storage has insufficient free space')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = [os.open(root, flags)]
    try:
        for name in ('users', str(user_id)):
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptors[-1])
            except FileExistsError:
                pass
            descriptors.append(os.open(name, flags, dir_fd=descriptors[-1]))
        info = os.fstat(descriptors[-1])
        if info.st_uid != os.geteuid():
            raise ValueError('Linux shell home is not owned by the API service')
        return validate_linux_home({
            'path': str(root/'users'/str(user_id)), 'device': info.st_dev, 'inode': info.st_ino,
        }, user_id)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
