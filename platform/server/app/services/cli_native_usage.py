"""Bounded metadata inventory of the explicitly configured WSL private root."""
import stat
from .cli_private_paths import native_root
from .storage_usage import MeasurementUnavailable,_ordinary


def link_entry(info):
    # WSL's 9P server exposes Linux symlinks as LX_SYMLINK reparse entries
    # (0xA000001D); Windows Python may report their mode as a regular file.
    # Other reparse types stay rejected. This is used only for tree entries;
    # configured roots and user namespace directories must remain ordinary.
    return stat.S_ISLNK(info.st_mode) or (
        getattr(info,'st_file_attributes',0)&0x400
        and getattr(info,'st_reparse_tag',0)==0xA000001D)


def fingerprint(path,counter):
    counter.check()
    try:
        info=_ordinary(path)
        if not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True)!=path:
            raise MeasurementUnavailable('Native CLI state root is redirected')
    except OSError as error:
        # A configured, unavailable root is not an absent user's empty tree.
        raise MeasurementUnavailable('Native CLI state root is unavailable') from error
    counter.check()
    return info.st_dev,info.st_ino


def _missing(path):
    try:
        _ordinary(path)
    except FileNotFoundError:
        return True
    return False


def _namespace_absent(root,user_id,counter):
    users=root/'users'
    try:
        initial=_ordinary(users)
    except FileNotFoundError:
        counter.check()
        return _missing(users)
    if not stat.S_ISDIR(initial.st_mode):
        raise MeasurementUnavailable('Native CLI user namespace is not a directory')
    folder=users/str(user_id)
    if not _missing(folder):
        return False
    counter.check()
    absent=_missing(folder)
    current=_ordinary(users)
    if not stat.S_ISDIR(current.st_mode) or (initial.st_dev,initial.st_ino)!=(current.st_dev,current.st_ino):
        raise MeasurementUnavailable('Native CLI user namespace changed')
    return absent


def scan(settings,counter,*,user_id=None):
    if user_id is not None and (type(user_id) is not int or user_id<=0):
        raise ValueError('Invalid native storage owner')
    selected=native_root(settings)
    if selected is None:return None
    before=fingerprint(selected,counter)
    # Account admission can prove an absent namespace through the configured
    # WSL filesystem. A present namespace still needs the owned Linux reader.
    if user_id is None or not _namespace_absent(selected,user_id,counter):
        from .cli_native_inventory import scan as native_inventory
        native_inventory(settings,counter,user_id=user_id,root_inode=before[1])
    if fingerprint(selected,counter)!=before or native_root(settings)!=selected:
        raise MeasurementUnavailable('Native CLI state root changed')
    return selected
