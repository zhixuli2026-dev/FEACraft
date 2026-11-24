"""仅复制文件内容，不复制时间戳、所有者或权限（供 MPI 暂存/回传）。"""
import os
import shutil
import sys


def copy_contents(source: str, destination: str):
    source = os.path.abspath(source)
    destination = os.path.abspath(destination)
    for root, _dirs, files in os.walk(source):
        relative = os.path.relpath(root, source)
        target = os.path.join(destination, relative)
        os.makedirs(target, exist_ok=True)
        for name in files:
            shutil.copyfile(os.path.join(root, name), os.path.join(target, name))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: mpi_copy.py SOURCE_DIR DEST_DIR")
    copy_contents(sys.argv[1], sys.argv[2])
