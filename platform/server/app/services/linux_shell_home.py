"""Provision and measure native Linux account homes in private storage."""

import errno
import os
from pathlib import Path
import shutil
import stat

from ..config import get_settings
from .case_write import CaseWriteConflict
from .cli_private_paths import root as cli_state_root
from .execution_budget import Budget
from .execution_storage import Watch, validate
from .storage_usage import Counter
from .terminal_protocol import validate_linux_home


def select(settings, user_id):
    hard = settings.linux_shell_require_project_quota
    if not settings.linux_shell_state_root:
        if hard:
            raise CaseWriteConflict('linux_home_quota_unverified', 'Linux 账户 HOME 缺少硬配额状态卷')
        return None
    if type(user_id) is not int or user_id <= 0:
        raise ValueError('Linux shell home requires a registered account')
    root = Path(settings.linux_shell_state_root)
    public = Path(settings.storage_fs_root).resolve()
    cli = cli_state_root(settings)
    if (not root.is_absolute() or root != root.resolve() or root == Path('/')
            or any(root.is_relative_to(folder) or folder.is_relative_to(root) for folder in (public, cli))):
        raise ValueError('Linux shell homes require a separate private volume path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        if not hard:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptors.append(os.open(root, flags))
        if shutil.disk_usage(root).free < settings.node_storage_min_free_mb * 1024**2:
            raise CaseWriteConflict('node_storage_full', 'Linux shell home storage has insufficient free space')
        for name in ('users', str(user_id)):
            if not hard:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
            descriptors.append(os.open(name, flags, dir_fd=descriptors[-1]))
        info = os.fstat(descriptors[-1])
        if info.st_uid != os.geteuid():
            raise ValueError('Linux shell home is not owned by the API service')
        selected = {
            'path': str(root/'users'/str(user_id)), 'device': info.st_dev, 'inode': info.st_ino,
        }
        if hard:
            from .linux_home_quota import measure
            selected['hard_quota_bytes'] = settings.execution_workspace_limit_mb * 1024**2
            measure(descriptors[0], descriptors[-1], user_id, selected['hard_quota_bytes'])
        return validate_linux_home(selected, user_id)
    except CaseWriteConflict:
        raise
    except (OSError, ValueError) as error:
        if hard:
            raise CaseWriteConflict('linux_home_quota_unverified', '无法核验 Linux 账户 HOME 的硬配额') from error
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class HomeWatch(Watch):
    def __init__(self, selection, policy):
        selected = validate_linux_home(selection)
        path = Path(selected['path'])
        budget = Budget(path.name, float('inf'), 0, storage_policy=validate(policy))
        super().__init__(budget, path, base=path.parent.parent, scope='linux_account_home')
        self.identity = (selected['device'], selected['inode'])
        self.hard_quota = selected.get('hard_quota_bytes')
        if self.hard_quota is not None and self.hard_quota != policy['max_workspace_bytes']:
            raise ValueError('Linux HOME hard limit differs from the admitted storage policy')

    def _snapshot(self, deadline):
        counter = Counter(get_settings(), deadline, None)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def scan(directory):
            with os.scandir(directory) as entries:
                for entry in entries:
                    counter.check()
                    try:
                        info = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # The account can remove transient entries while active.
                    is_dir = stat.S_ISDIR(info.st_mode)
                    counter.add(0 if is_dir else info.st_size)
                    if not is_dir:
                        continue
                    try:
                        child = os.open(entry.name, flags, dir_fd=directory)
                    except OSError as error:
                        if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                            continue  # Deletion or a replacement by a non-directory.
                        raise
                    try:
                        scan(child)
                    finally:
                        os.close(child)

        counter.check()
        descriptor = os.open(self.root, flags)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != self.identity or info.st_uid != os.geteuid():
                raise ValueError('Linux account home identity changed')
            if self.hard_quota is None:
                try:
                    scan(descriptor)
                except RecursionError as error:
                    raise ValueError('Linux account home exceeds measurable directory depth') from error
                volumes = os.fstatvfs(descriptor)
                size, free = counter.bytes, volumes.f_bavail * volumes.f_frsize
            else:
                from .linux_home_quota import measure
                volume = os.open(self.base, flags)
                try:
                    size, free = measure(volume, descriptor, int(self.root.name), self.hard_quota)
                finally:
                    os.close(volume)
            info = self.root.lstat()
            if (info.st_dev, info.st_ino) != self.identity:
                raise ValueError('Linux account home identity changed')
            return size, free, True
        finally:
            os.close(descriptor)
