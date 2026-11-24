"""Fixed native state initializer; invoked as nobody, with no credential values.

The operator provisions a Linux filesystem root owned by that worker. Windows
drive mounts cannot reliably support Codex's chmod/flock state requirements.
This module deliberately uses only the standard library in WSL.
"""
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time
from contextlib import contextmanager


def native_root(value):
    if not isinstance(value, str):
        raise ValueError('WSL CLI state requires an explicit native Linux directory')
    path = PurePosixPath(value)
    if (not path.is_absolute() or str(path) != value or len(path.parts) < 3
            or '..' in path.parts or path.parts[1] in {'mnt', 'proc', 'sys', 'dev'} or '\\' in value):
        raise ValueError('WSL CLI state requires an explicit native Linux directory')
    return path


def mounted_path(value, distro):
    """Map only drive paths or this selected distribution's UNC paths."""
    normalized = str(value).replace('\\', '/')
    for prefix in ('//wsl.localhost/', '//wsl$/'):
        if normalized.lower().startswith(prefix):
            parts = normalized[len(prefix):].split('/', 1)
            if len(parts) != 2 or parts[0].lower() != distro.lower():
                raise ValueError('Private CLI state belongs to another distribution')
            return '/' + parts[1]
    from .owned_process import wsl_path
    return wsl_path(value)


def selection(root, relative, configuration, expected):
    """Freeze initialization inputs without starting a process or writing Linux state."""
    value = {'root': root, 'relative': relative, 'configuration': configuration, 'owner': expected}
    validate_selection(value)
    return value


def validate_selection(value):
    if not isinstance(value, dict) or set(value) != {'root', 'relative', 'configuration', 'owner'}:
        raise ValueError('Invalid private state initialization selection')
    native_root(value['root'])
    relative = value['relative']
    if (not isinstance(relative, str) or not relative or len(relative) > 1024
            or any(not part or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in part)
                   for part in relative.split('/'))):
        raise ValueError('Invalid private account path')
    if (not isinstance(value['configuration'], str) or len(value['configuration'].encode('utf8')) > 16384
            or not isinstance(value['owner'], dict)):
        raise ValueError('Invalid private configuration or owner')
    return value


def home_path(request):
    validate_selection(request)
    return request['root'] + '/' + request['relative'] + '/home'


def windows_home(request, distro):
    return Path('\\\\wsl.localhost\\' + distro + home_path(request).replace('/', '\\'))


def initialization_payload(request):
    validate_selection(request)
    configuration=request['configuration'].encode('utf8')
    marker=json.dumps(request['owner'],sort_keys=True).encode('utf8')
    if len(marker)>16384:raise ValueError('Private native owner exceeds its initialization bound')
    return configuration,marker


def _present(path):
    try:path.lstat();return True
    except FileNotFoundError:return False


def _directory(path):
    info=path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid()
            or stat.S_IMODE(info.st_mode)!=0o700 or path.resolve()!=path):
        raise ValueError('Private state directory ownership or permissions changed')


def _file(info,limit):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
            or stat.S_IMODE(info.st_mode)!=0o600 or not 0<=info.st_size<=limit):
        raise ValueError('Private state file ownership, permissions or size changed')


def _sync(directory):
    descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def _read(path,limit):
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info=os.fstat(descriptor);_file(info,limit)
        value=os.read(descriptor,limit+1)
        current=path.lstat()
        if ((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_nlink)
                !=(current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns,current.st_nlink)
                or len(value)!=info.st_size):raise ValueError('Private state changed during reading')
        return value
    finally:os.close(descriptor)


def _prefix(path,expected):
    if _present(path) and not expected.startswith(_read(path,len(expected))):
        raise ValueError('Private initialization fragment differs from its frozen input')


def _resume_file(path,expected):
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        info=os.fstat(descriptor);_file(info,len(expected))
        before=os.read(descriptor,len(expected)+1)
        if not expected.startswith(before) or len(before)!=info.st_size:
            raise ValueError('Private initialization fragment changed before continuation')
        os.lseek(descriptor,0,os.SEEK_END)
        position=len(before)
        while position<len(expected):
            count=os.write(descriptor,expected[position:])
            if count<=0:raise OSError('Private initialization write made no progress')
            position+=count
        os.fsync(descriptor)
    finally:os.close(descriptor)
    _sync(path.parent)


def _publish(source,target):
    # Same no-replace primitive as fs_directory.move_new, kept standalone for
    # the distribution's stdlib-only initializer and used for files and dirs.
    import ctypes
    library=ctypes.CDLL(None,use_errno=True)
    rename=getattr(library,'renameat2',None)
    if rename is None:raise ValueError('Native initialization requires no-replace publication')
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype=ctypes.c_int
    if rename(-100,os.fsencode(source),-100,os.fsencode(target),1)!=0:
        code=ctypes.get_errno();raise OSError(code,os.strerror(code),str(target))
    _sync(source.parent);_sync(target.parent)


@contextmanager
def _account_lock(account):
    import fcntl
    path=account/'.initialize.lock'
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        _file(os.fstat(descriptor),0)
        deadline=time.monotonic()+10
        while True:
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise TimeoutError('Private initialization is already in progress')
                time.sleep(.02)
        _directory(account);_sync(account)
        yield
    finally:os.close(descriptor)


def _entries(directory,allowed):
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name not in allowed:raise ValueError('Private initialization contains unknown entries')


def _initialize_locked(account,request,configuration,marker_bytes):
    signature=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode('utf8')).hexdigest()
    pending=account/('.initialize-'+signature)
    home,marker=account/'home',account/'owner.json'
    if _present(home):
        _directory(home)
        if (_read(home/'config.toml',16384)!=configuration
                or json.loads(_read(marker,16384))!=request['owner']):
            raise ValueError('Private state configuration or owner changed')
        if _present(pending):
            _directory(pending);_entries(pending,set());pending.rmdir();_sync(account)
    else:
        # A marker without this operation's staging directory is not evidence
        # that an existing user's home can be reconstructed.
        if _present(marker) and not _present(pending):
            raise ValueError('Existing private owner has no recoverable initialization')
        with os.scandir(account) as entries:
            for index,entry in enumerate(entries):
                if index>=256:raise ValueError('Private account inventory exceeds its initialization bound')
                if entry.name.startswith('.initialize-') and entry.name!=pending.name:
                    raise ValueError('Another private initialization has different frozen inputs')
        if not _present(pending):pending.mkdir(mode=0o700);_sync(account)
        _directory(pending);_entries(pending,{'owner.json','home'})
        staged_owner,staged_home=pending/'owner.json',pending/'home'
        if _present(marker):
            if json.loads(_read(marker,16384))!=request['owner'] or _present(staged_owner):
                raise ValueError('Private owner publication is ambiguous')
        else:_prefix(staged_owner,marker_bytes)
        if _present(staged_home):
            _directory(staged_home);_entries(staged_home,{'config.toml'})
            _prefix(staged_home/'config.toml',configuration)
        # Validate all existing fragments before appending any new bytes.
        if not _present(marker):_resume_file(staged_owner,marker_bytes)
        if not _present(staged_home):staged_home.mkdir(mode=0o700);_sync(pending)
        _resume_file(staged_home/'config.toml',configuration)
        if not _present(marker):_publish(staged_owner,marker)
        _publish(staged_home,home)
        _entries(pending,set());pending.rmdir();_sync(account)
    temporary=account/'tmp'
    if not _present(temporary):temporary.mkdir(mode=0o700);_sync(account)
    _directory(temporary)
    return {'home':str(home),'uid':os.getuid()}


def initialize(request):
    validate_selection(request)
    configuration_bytes,marker_bytes=initialization_payload(request)
    if os.getuid() != 65534 or set(request) != {'root', 'relative', 'configuration', 'owner'}:
        raise ValueError('Native state requires the selected non-root worker')
    root = Path(str(native_root(request['root'])))
    relative = PurePosixPath(request['relative'])
    if relative.is_absolute() or str(relative) != request['relative'] or '..' in relative.parts:
        raise ValueError('Invalid private account path')
    configuration = request['configuration']
    if not isinstance(configuration, str) or len(configuration.encode('utf8')) > 16384:
        raise ValueError('Invalid private configuration')

    _directory(root)
    account = root
    for part in relative.parts:
        if not part or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in part):
            raise ValueError('Invalid account path component')
        account = account / part
        try: account.mkdir(mode=0o700)
        except FileExistsError: pass
        _directory(account)
    with _account_lock(account):
        return _initialize_locked(account,request,configuration_bytes,marker_bytes)


if __name__ == '__main__':
    try:
        payload = sys.stdin.buffer.read(32769)
        if len(payload) > 32768: raise ValueError('Oversized state request')
        print(json.dumps(initialize(json.loads(payload))))
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit(125)
