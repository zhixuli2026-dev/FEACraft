"""H10：building-UUID 锁、WAL/checkpoint 与过期锁恢复。"""
import os
from pathlib import Path
import shutil
import tempfile
import time

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-index-lock-"))
os.environ.update(VIBEFLOW_STORAGE_BACKEND="fs", VIBEFLOW_STORAGE_FS_ROOT=str(ROOT),
                  VIBEFLOW_INDEX_ROOT=str(ROOT / "index"))
from app.services import indexsvc

CASE = ROOT / "case"
CASE.mkdir()
(CASE / "a.txt").write_text("alpha content", encoding="utf-8")
(CASE / "b.txt").write_text("beta content", encoding="utf-8")
try:
    first = indexsvc.rebuild(42, "case")
    assert first["building_uuid"], first
    status = indexsvc.status(42)
    assert status["building_uuid"] == first["building_uuid"], status
    assert not (indexsvc.INDEX_DIR / "case-42.building").exists()
    assert indexsvc.search(42, "alpha")["hits"], "首建索引不可搜索"
    print("1. building_uuid、锁清理和 WAL 索引可搜索 [OK]")

    lock = indexsvc.INDEX_DIR / "case-42.building"
    lock.write_text("other-process", encoding="ascii")
    try:
        indexsvc.rebuild(42, "case")
        raise AssertionError("活跃锁不应允许第二个重建")
    except RuntimeError as error:
        assert "另一个索引构建" in str(error), error
    assert indexsvc.search(42, "beta")["hits"], "冲突时原索引不可读"
    print("2. 活跃锁拒绝且原索引保持可读 [OK]")

    old = time.time() - 601
    os.utime(lock, (old, old))
    second = indexsvc.rebuild(42, "case")
    assert second["building_uuid"] != first["building_uuid"]
    assert not lock.exists()
    stale = list((indexsvc.INDEX_DIR).glob("case-42.stale-*"))
    assert stale, "过期锁未保留证据"
    assert indexsvc.search(42, "alpha")["hits"]
    print("3. 过期锁恢复构建且保留 stale 证据 [OK]")
    print("\n=== 索引事务/锁/WAL/过期恢复冒烟通过 ===")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)
