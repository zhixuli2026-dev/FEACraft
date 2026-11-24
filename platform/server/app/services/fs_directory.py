"""No-overwrite directory publication on the supported local filesystems."""
import errno
import os
from pathlib import Path
import sys
import time


class DirectoryMoveError(ValueError):
    pass


def move_new(source: Path, target: Path, *, root: Path):
    return _move_new(source,target,root=root,directory=True)


def move_file_new(source: Path,target: Path,*,root: Path):
    """Publish a fully fsynced file without replacing an existing name."""
    return _move_new(source,target,root=root,directory=False)


def _move_new(source: Path,target: Path,*,root: Path,directory: bool):
    root, source, target = root.resolve(), source.resolve(), target.resolve()
    if any(path == root or not path.is_relative_to(root) for path in (source, target)):
        raise DirectoryMoveError("目录移动必须保持在指定存储根目录内")
    if target.exists():
        raise DirectoryMoveError("目标目录已存在，拒绝覆盖")
    if directory and not source.is_dir():
        raise DirectoryMoveError("源目录不存在")
    if not directory and not source.is_file():
        raise DirectoryMoveError("源文件不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            deadline = time.monotonic()+1
            while True:
                try:
                    os.rename(source, target)
                    break
                except PermissionError as error:
                    if target.exists():
                        raise DirectoryMoveError("目标目录已存在，拒绝覆盖") from error
                    if error.winerror not in {5, 32} or time.monotonic() >= deadline:
                        raise DirectoryMoveError("目录仍被占用或无移动权限，请关闭相关程序后重试") from error
                    time.sleep(0.02)
        elif sys.platform.startswith("linux"):
            import ctypes
            library = ctypes.CDLL(None, use_errno=True)
            rename = getattr(library, "renameat2", None)
            if rename is None:
                raise DirectoryMoveError("当前运行时不支持无覆盖目录移动")
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), str(target))
        else:
            raise DirectoryMoveError("当前平台未提供无覆盖目录移动")
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise DirectoryMoveError("目标目录已存在，拒绝覆盖") from error
        raise


def remove_owned(path: Path, *, root: Path):
    """Only for a directory the caller created exclusively and still owns."""
    import shutil
    root = root.resolve()
    def remove():
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError("Cleanup target escapes the operation's root")
        if path.is_symlink():
            raise ValueError("Cleanup target was replaced by a link")
        if path.exists():
            shutil.rmtree(path)
    retry_sharing_violation(remove)


def retry_sharing_violation(operation):
    deadline=time.monotonic()+3
    attempt=0
    while True:
        try:
            operation()
            return
        except OSError as error:
            # Native compilers and indexers can briefly retain Windows handles.
            # Revalidate ownership on every retry; never retry unrelated errors.
            if getattr(error,'winerror',None) not in {32,33} or time.monotonic()>=deadline:
                raise
            from .request_deadline import current
            budget=current()
            if budget is not None:budget.check()
            attempt+=1
            time.sleep(min(.05*attempt,.25,max(0,deadline-time.monotonic())))
