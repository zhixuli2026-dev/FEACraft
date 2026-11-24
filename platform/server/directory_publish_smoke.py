"""Stdlib-only native directory publication and owned-cleanup boundaries."""
import json
import os
import shutil
from pathlib import Path
import tempfile
from unittest.mock import patch

from app.services.fs_directory import DirectoryMoveError, move_new, remove_owned


def main():
    evidence = Path(tempfile.mkdtemp(prefix="directory-publication-"))
    root = evidence / "files"
    root.mkdir()
    checks = []

    def source(name):
        path = root / name
        path.mkdir()
        (path / "input").write_bytes(b"complete inputs")
        return path

    original = source("source")
    target = root / "published"
    move_new(original, target, root=root)
    assert not original.exists() and (target / "input").read_bytes() == b"complete inputs"
    checks.append("complete_directory_published_without_copying_or_replacing_existing_output")

    for empty in (False, True):
        original = source("racing-source-"+str(empty))
        target = root / ("external-target-"+str(empty))
        target.mkdir()
        if not empty:
            (target / "KEEP").write_bytes(b"external evidence")
        exists = Path.exists

        def stale_exists(path):
            return False if path == target else exists(path)

        with patch.object(Path, "exists", stale_exists):
            try:
                move_new(original, target, root=root)
                raise AssertionError("OS publication replaced an externally created directory")
            except DirectoryMoveError:
                pass
        assert (original / "input").read_bytes() == b"complete inputs"
        assert set(p.name for p in target.iterdir()) == (set() if empty else {"KEEP"})
    checks.append("native_no_replace_survives_stale_exists_for_empty_and_nonempty_external_targets")

    outside = evidence / "outside"
    outside.mkdir()
    (outside / "KEEP").write_bytes(b"outside boundary")
    for candidate in (root, outside):
        try:
            remove_owned(candidate, root=root)
            raise AssertionError("Cleanup crossed the operation boundary")
        except ValueError:
            pass
    assert (outside / "KEEP").read_bytes() == b"outside boundary"
    owned = source("owned-stage")
    remove_owned(owned, root=root)
    assert not owned.exists()
    checks.append("cleanup_removes_only_owned_subdirectory_and_refuses_root_or_outside_paths")

    owned = source('cleanup-sharing-retry')
    rmtree = shutil.rmtree
    calls = []
    def busy_cleanup(path):
        calls.append(path)
        if len(calls)==1:
            error=PermissionError('controlled Windows sharing violation')
            error.winerror=32
            raise error
        return rmtree(path)
    with patch.object(shutil,'rmtree',busy_cleanup):
        remove_owned(owned,root=root)
    assert len(calls)==2 and not owned.exists()
    checks.append('owned_cleanup_retries_transient_windows_sharing_violation')

    owned = source('cleanup-permission-denied')
    error=PermissionError('controlled unrelated denial')
    error.winerror=5
    with patch.object(shutil,'rmtree',side_effect=error) as deletion:
        try:
            remove_owned(owned,root=root)
            raise AssertionError('Cleanup swallowed unrelated deletion failure')
        except PermissionError:
            pass
        assert deletion.call_count==1 and owned.exists()
    remove_owned(owned,root=root)
    checks.append('owned_cleanup_preserves_unrelated_failures_without_retry')

    for candidate in (root, outside):
        try:
            move_new(candidate, root / "not-published", root=root)
            raise AssertionError("Move accepted root or external source")
        except DirectoryMoveError:
            pass
    original = source("inside")
    try:
        move_new(original, outside / "not-published", root=root)
        raise AssertionError("Move accepted external destination")
    except DirectoryMoveError:
        pass
    checks.append("move_rejects_root_and_external_sources_or_destinations")

    if os.name == "nt":
        original = source("temporarily-busy")
        destination = root / "retry-target"
        rename = os.rename
        calls = []

        def busy_once(src, dst):
            calls.append(1)
            if len(calls) == 1:
                error = PermissionError("test sharing violation")
                error.winerror = 32
                raise error
            return rename(src, dst)

        with patch.object(os, "rename", busy_once):
            move_new(original, destination, root=root)
        assert len(calls) == 2 and (destination / "input").exists()
        checks.append("windows_transient_sharing_violation_retries_no_replace_rename")

    result = {"passed": True, "count": len(checks), "platform": os.name, "checks": checks,
              "evidence": str(evidence)}
    (evidence / "result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
