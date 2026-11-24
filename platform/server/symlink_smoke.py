"""F32：prefix 根、最近存在父目录和符号链接边界；只操作临时目录。"""
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-symlink-"))
STORAGE = ROOT / "files"
os.environ["VIBEFLOW_STORAGE_BACKEND"] = "fs"
os.environ["VIBEFLOW_STORAGE_FS_ROOT"] = str(STORAGE)

from app.storage import _fs_path

PREFIX = "workspaces/1/cases/1"
BASE = STORAGE / "workspaces/1/cases/1"
OTHER_CASE = STORAGE / "workspaces/1/cases/2"
OUTSIDE = ROOT / "outside"
SIBLING = ROOT / "files-evil"
for directory in (BASE, OTHER_CASE, OUTSIDE, SIBLING):
    directory.mkdir(parents=True, exist_ok=True)
(BASE / "real.txt").write_text("internal", encoding="utf-8")
(OTHER_CASE / "secret.txt").write_text("other case", encoding="utf-8")
(OUTSIDE / "secret.txt").write_text("outside", encoding="utf-8")
(SIBLING / "secret.txt").write_text("sibling", encoding="utf-8")
(BASE / "internal-link.txt").symlink_to(BASE / "real.txt")
(BASE / "outside-link.txt").symlink_to(OUTSIDE / "secret.txt")
(BASE / "case-link").symlink_to(OTHER_CASE, target_is_directory=True)
(BASE / "outside-dir").symlink_to(OUTSIDE, target_is_directory=True)
(BASE / "sibling-dir").symlink_to(SIBLING, target_is_directory=True)
(BASE / "broken-link").symlink_to(OUTSIDE / "missing" / "new.txt")


def expect_blocked(label, path):
    try:
        _fs_path(PREFIX, path)
        raise AssertionError(f"{label} 应拒绝: {path}")
    except ValueError:
        print(f"{label} [OK]")


try:
    internal = _fs_path(PREFIX, "internal-link.txt")
    assert internal.read_text(encoding="utf-8") == "internal", internal
    assert internal.is_relative_to(BASE.resolve()), internal
    print("1. 算例内部符号链接允许且仍位于 case 根 [OK]")
    expect_blocked("2. 文件符号链接指向算例外拒绝", "outside-link.txt")
    expect_blocked("3. 目录符号链接指向其他 case 拒绝", "case-link/secret.txt")
    expect_blocked("4. 链接目录下不存在目标也拒绝", "outside-dir/new/nested.txt")
    expect_blocked("5. storage 根相似前缀不混淆", "sibling-dir/secret.txt")
    expect_blocked("6.  broken 链接的解析目标越界拒绝", "broken-link")
    expect_blocked("7. 普通 .. 穿越继续拒绝", "../cases/2/secret.txt")
    print("\n=== prefix/最近父目录/符号链接边界冒烟通过 ===")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)
