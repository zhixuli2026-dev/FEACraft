"""fs 回收站：case/workspace 可恢复移动；数据库行保持不变。

同一fs根的进程间文件锁保护清单与移动；API另持有工作区/算例DB锁。
永久清除、分布式文件系统保证、崩溃日志和S3版本尚未提供。
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import asyncio
import json
import os
import errno
from pathlib import Path
import threading
import time
import sys
import uuid

from ..config import get_settings
from ..storage import _fs_path, _fs_root
from .async_ops import finish_before_cancel

_LOCK = threading.RLock()


class TrashError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class TrashBusy(TrashError):
    def __init__(self):
        super().__init__("回收站正在处理其他操作，请稍后重试", 409)


def _index_path() -> Path:
    return _fs_root().resolve() / "_trash" / "index.json"


@contextmanager
def _mutation_lock(*, deadline=None, blocking=True):
    """Only call from a worker thread. Lock file stays stable across index replaces."""
    deadline = time.monotonic()+30 if deadline is None else deadline
    acquired_thread = (_LOCK.acquire(timeout=max(0, deadline-time.monotonic())) if blocking
                       else _LOCK.acquire(blocking=False))
    if not acquired_thread:
        raise TrashBusy()
    try:
        path = _index_path().with_name("index.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            acquired = False
            try:
                while not acquired:
                    if time.monotonic() >= deadline:
                        raise TrashBusy()
                    stream.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except (BlockingIOError, PermissionError):
                        if not blocking or time.monotonic() >= deadline:
                            raise TrashBusy()
                        time.sleep(0.02)
                yield
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        _LOCK.release()


async def mutate(function, *args):
    """Don't release DB locks while a cancelled request's filesystem thread runs."""
    return await finish_before_cancel(asyncio.to_thread(function, *args))


async def wait_for_mutation():
    """Wait before taking workspace and case write locks."""
    deadline = time.monotonic() + 30

    def probe():
        with _mutation_lock(deadline=deadline, blocking=False):
            pass

    while True:
        try:
            await mutate(probe)
            return
        except TrashBusy:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(.05, remaining))


def _load() -> dict:
    path = _index_path()
    if not path.is_file():
        return {"version": 1, "cases": {}, "workspaces": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrashError("回收站清单损坏，拒绝继续移动文件", 500) from error
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("cases"), dict) \
            or not isinstance(data.get("workspaces"), dict):
        raise TrashError("回收站清单格式无效", 500)
    return data


def _save(data: dict):
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _move(original_prefix: str, trash_prefix: str):
    if get_settings().storage_backend != "fs":
        raise TrashError("回收站当前仅支持 fs 存储", 409)
    source = _fs_path(original_prefix, ".")
    target = _fs_path(trash_prefix, ".")
    if not source.is_dir():
        raise TrashError(f"算例文件目录不存在: {original_prefix}", 409)
    if target.exists():
        raise TrashError("回收站目标已存在，拒绝覆盖", 409)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            # Short-lived readers (including filesystem scanners) may still have
            # handles open after a save/restore. Every retry uses the atomic,
            # non-overwriting Windows rename, never replace/copy/delete.
            deadline = time.monotonic()+1
            while True:
                try:
                    os.rename(source, target)
                    break
                except PermissionError as error:
                    if target.exists():
                        raise TrashError("回收站目标已存在，拒绝覆盖", 409) from error
                    if error.winerror not in {5, 32} or time.monotonic() >= deadline:
                        raise TrashError("目录仍被占用或无移动权限，请关闭相关程序后重试", 409) from error
                    time.sleep(0.02)
        elif sys.platform.startswith("linux"):
            import ctypes
            library = ctypes.CDLL(None, use_errno=True)
            rename = getattr(library, "renameat2", None)
            if rename is None:
                raise TrashError("当前运行时不支持无覆盖目录移动", 409)
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), str(target))
        else:
            raise TrashError("当前平台未提供无覆盖目录移动", 409)
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise TrashError("回收站目标已存在，拒绝覆盖", 409) from error
        raise


def is_case_deleted(case_id: int) -> bool:
    # Atomic index replacement gives readers a complete old/new snapshot. Writers
    # recheck after DB locking, once any competing directory move has finished.
    return str(case_id) in _load()["cases"]


def is_workspace_deleted(workspace_id: int) -> bool:
    return str(workspace_id) in _load()["workspaces"]


def case_unavailable(case_id: int, workspace_id: int) -> bool:
    data = _load()
    return str(case_id) in data["cases"] or str(workspace_id) in data["workspaces"]


def list_deleted(user_id: int) -> dict:
    data = _load()
    cases = [{key: value for key, value in entry.items() if key in ("id", "name", "deleted_at")}
             for entry in data["cases"].values() if entry["user_id"] == user_id]
    workspaces = [{key: value for key, value in entry.items() if key in ("id", "name", "deleted_at")}
                  for entry in data["workspaces"].values() if entry["user_id"] == user_id]
    return {"cases": cases, "workspaces": workspaces}


def delete_case(case, user_id: int) -> dict:
    with _mutation_lock(blocking=False):
        data = _load()
        if str(case.id) in data["cases"]:
            raise TrashError("算例已在回收站", 409)
        trash_prefix = f"_trash/users/{user_id}/cases/{case.id}-{uuid.uuid4().hex}"
        entry = {"id": case.id, "name": case.name, "user_id": user_id,
                 "original_prefix": case.s3_prefix, "trash_prefix": trash_prefix,
                 "deleted_at": datetime.now(timezone.utc).isoformat()}
        _move(case.s3_prefix, trash_prefix)
        data["cases"][str(case.id)] = entry
        try:
            _save(data)
        except Exception:
            _move(trash_prefix, case.s3_prefix)
            raise
        return entry


def restore_case(case, user_id: int) -> dict:
    with _mutation_lock(blocking=False):
        data = _load()
        entry = data["cases"].get(str(case.id))
        if entry is None or entry["user_id"] != user_id:
            raise TrashError("回收站中不存在该算例", 404)
        if entry.get("id") != case.id or entry.get("original_prefix") != case.s3_prefix:
            raise TrashError("回收站算例记录与当前目录不一致，拒绝移动", 409)
        _move(entry["trash_prefix"], entry["original_prefix"])
        del data["cases"][str(case.id)]
        try:
            _save(data)
        except Exception:
            _move(entry["original_prefix"], entry["trash_prefix"])
            raise
        return entry


def delete_workspace(workspace, cases: list, user_id: int) -> dict:
    with _mutation_lock(blocking=False):
        data = _load()
        if str(workspace.id) in data["workspaces"]:
            raise TrashError("工作区已在回收站", 409)
        if any(str(case.id) in data["cases"] for case in cases):
            raise TrashError("请先恢复单独删除的算例，再删除整个工作区", 409)
        root = f"_trash/users/{user_id}/workspaces/{workspace.id}-{uuid.uuid4().hex}"
        entry = {"id": workspace.id, "name": workspace.name, "user_id": user_id,
                 "deleted_at": datetime.now(timezone.utc).isoformat(), "cases": []}
        moved = []
        try:
            for case in cases:
                trash_prefix = f"{root}/cases/{case.id}"
                _move(case.s3_prefix, trash_prefix)
                moved.append((case.s3_prefix, trash_prefix))
                entry["cases"].append({"id": case.id, "original_prefix": case.s3_prefix,
                                       "trash_prefix": trash_prefix})
            data["workspaces"][str(workspace.id)] = entry
            _save(data)
        except Exception:
            for original, trashed in reversed(moved):
                if _fs_path(trashed, ".").is_dir() and not _fs_path(original, ".").exists():
                    _move(trashed, original)
            raise
        return entry


def restore_workspace(workspace, user_id: int, cases: list) -> dict:
    with _mutation_lock(blocking=False):
        data = _load()
        entry = data["workspaces"].get(str(workspace.id))
        if entry is None or entry["user_id"] != user_id:
            raise TrashError("回收站中不存在该工作区", 404)
        expected = {case.id: case.s3_prefix for case in cases}
        items = entry.get("cases", [])
        if (entry.get("id") != workspace.id or not isinstance(items, list) or
                len(items) != len(expected) or any(not isinstance(item, dict) for item in items) or
                any(type(item.get("id")) is not int for item in items) or
                {item.get("id") for item in items} != set(expected) or
                any(item.get("original_prefix") != expected[item["id"]] for item in items)):
            raise TrashError("回收站工作区成员与当前记录不一致，拒绝移动", 409)
        moved = []
        try:
            for item in entry["cases"]:
                _move(item["trash_prefix"], item["original_prefix"])
                moved.append(item)
            del data["workspaces"][str(workspace.id)]
            _save(data)
        except Exception:
            for item in reversed(moved):
                if _fs_path(item["original_prefix"], ".").is_dir() \
                        and not _fs_path(item["trash_prefix"], ".").exists():
                    _move(item["original_prefix"], item["trash_prefix"])
            raise
        return entry
