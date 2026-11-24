"""Offline OS-lock, no-overwrite and cancelled-request lifetime checks.

Only configuration is stubbed; the actual trash/storage filesystem code runs on
the host OS. No database or optional third-party package is needed on Linux.
"""
import asyncio
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def configure(root):
    config = ModuleType("app.config")
    settings = SimpleNamespace(storage_backend="fs", storage_fs_root=str(root))
    config.get_settings = lambda: settings
    sys.modules["app.config"] = config
    from app.services import trash
    return trash


async def cancellation_checks(trash):
    entered, release = threading.Event(), threading.Event()

    def worker():
        entered.set()
        assert release.wait(5)
        return "finished"

    task = asyncio.create_task(trash.mutate(worker))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done(), "Cancellation released the caller before filesystem work ended"
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
    finally:
        release.set()
    try:
        await task
        raise AssertionError("Cancellation was swallowed")
    except asyncio.CancelledError:
        pass
    assert await trash.mutate(lambda: "success") == "success"


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--lock-owner":
        trash = configure(Path(sys.argv[2]))
        with trash._mutation_lock():
            os._exit(23)  # No finally/unlock; the OS must release the lock.

    report = Path(tempfile.mkdtemp(prefix="trash-control-"))
    trash = configure(report / "files")
    checks = []
    source, target = report / "files/source", report / "files/target"
    source.mkdir(parents=True)
    marker = source / "marker.txt"
    marker.write_bytes(b"original")

    def conflict(function):
        try:
            function()
            raise AssertionError("Conflicting move succeeded")
        except trash.TrashError as error:
            assert error.status_code == 409, error

    for occupied in (False, True):
        target.mkdir()
        if occupied:
            (target / "external.txt").write_bytes(b"external")
        conflict(lambda: trash._move("source", "target"))
        assert marker.read_bytes() == b"original"
        if occupied:
            assert (target / "external.txt").read_bytes() == b"external"
            (target / "external.txt").unlink()
        target.rmdir()
    checks.append("existing_empty_and_nonempty_targets_never_overwritten")

    # Inject a target after preflight but before the actual OS rename call.
    if os.name == "nt":
        rename = os.rename

        def raced_rename(src, dst):
            Path(dst).mkdir()
            return rename(src, dst)

        with patch.object(trash.os, "rename", raced_rename):
            conflict(lambda: trash._move("source", "target"))
    elif sys.platform.startswith("linux"):
        import ctypes
        original_cdll = ctypes.CDLL

        def raced_library(*args, **kwargs):
            library = original_cdll(*args, **kwargs)
            target.mkdir()
            return library

        with patch.object(ctypes, "CDLL", raced_library):
            conflict(lambda: trash._move("source", "target"))
    else:
        raise AssertionError("Run this check on a supported Windows/Linux host")
    assert target.is_dir() and marker.read_bytes() == b"original"
    target.rmdir()
    checks.append("target_created_after_preflight_refused_by_real_OS_rename")

    if os.name == "nt":
        rename = os.rename
        calls = []

        def temporarily_busy(src, dst):
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(errno.EACCES, "busy", str(src), 32)
            return rename(src, dst)

        with patch.object(trash.os, "rename", temporarily_busy):
            trash._move("source", "target")
        assert len(calls) == 2 and (target / "marker.txt").read_bytes() == b"original"
        trash._move("target", "source")
        with patch.object(trash.os, "rename", side_effect=PermissionError(errno.EACCES, "busy", str(source), 32)), \
                patch.object(trash.time, "monotonic", side_effect=[0, 2]):
            conflict(lambda: trash._move("source", "target"))
        assert marker.read_bytes() == b"original" and not target.exists()
        checks.append("transient_Windows_handles_retried_and_persistent_busy_move_returns_conflict")

    trash._move("source", "target")
    trash._move("target", "source")
    assert marker.read_bytes() == b"original"
    checks.append("real_directory_round_trip_preserves_bytes")

    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--lock-owner", str(report / "files")],
        capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert child.returncode == 23, child.stderr.decode("utf-8", "replace")
    start = time.monotonic()
    with trash._mutation_lock():
        assert time.monotonic()-start < 3
    assert (report / "files/_trash/index.lock").is_file()
    checks.append("process_exit_releases_OS_lock_despite_retained_lock_file")

    asyncio.run(cancellation_checks(trash))
    checks.append("repeated_request_cancellation_waits_for_filesystem_worker_before_propagation")

    case = SimpleNamespace(id=1, s3_prefix="source")
    workspace = SimpleNamespace(id=1)
    for items in ([{"id": [], "original_prefix": "source"}],
                  [{"id": 2, "original_prefix": "source"}],
                  [{"id": 1, "original_prefix": "other"}]):
        trash._save({"version": 1, "cases": {}, "workspaces": {
            "1": {"id": 1, "user_id": 1, "cases": items}}})
        conflict(lambda: trash.restore_workspace(workspace, 1, [case]))
        assert marker.read_bytes() == b"original"
    checks.append("malformed_or_changed_workspace_members_rejected_before_any_move")
    result = {"passed": True, "platform": sys.platform, "checks": checks, "count": len(checks), "evidence": str(report)}
    (report / "result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
