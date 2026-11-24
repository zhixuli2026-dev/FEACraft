"""Allowlisted, hashed source snapshots; never copies development data or private settings.

Restore always requires a new directory. This is a rollback/review baseline for
this workspace, which has no Git history; it is not a backup of user case files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
        "data", "dist", "backups", "shots", "test-results", "playwright-report",
        "wsl-vibeflow-linux", "seaweedfs", "s3data"}
EXTENSIONS = {".py", ".md", ".txt", ".json", ".toml", ".ini", ".mako", ".yml",
              ".yaml", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html",
              ".ps1", ".sh", ".svg", ".png", ".ico", ".woff", ".woff2",
              ".stl", ".step", ".geo", ".cfg", ".inp", ".csv", ".conf", ".su2",
              ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx", ".mk"}
NAMES = {"Dockerfile", ".dockerignore", ".gitignore", ".gitkeep", ".env.example", "Makefile", "makefile"}
MANIFEST = "source-baseline.json"


def sources(root: Path):
    instructions=root/'AGENTS.md'
    if instructions.is_file() and not instructions.is_symlink():yield instructions
    # Completion is distributable static product data, unlike files/audit/index/keys.
    for base in (root / "platform", root / ".github", root / "platform/server/data/completion"):
        if not base.exists():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".")
                             and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink() or (name.startswith(".env") and name != ".env.example"):
                    continue
                if (path.suffix.lower() in EXTENSIONS or name in NAMES or name.startswith("Dockerfile.")
                        or (path.parent.name == 'Make' and name in {'files', 'options'})):
                    yield path


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def create(destination: Path):
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    inventory = []
    # Exclusive create: a named baseline can never silently change.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sources(ROOT):
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            inventory.append({"path": relative, "bytes": len(data), "sha256": sha(data)})
            archive.writestr(relative, data)
        packages = subprocess.run([sys.executable, "-m", "pip", "list", "--format=json",
                                   "--disable-pip-version-check"], capture_output=True, check=True,
                                  text=True, encoding="utf-8")
        manifest = {"schema": "vibeflow.source-baseline.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(), "platform": platform.platform(),
            "packages": json.loads(packages.stdout), "files": inventory,
            "excluded": sorted(SKIP), "configuration": "Only .env.example; no private .env or data",
            "dependency_restore": "Use recorded requirements/package-lock and existing verified runtimes"}
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    digest = sha(destination.read_bytes())
    destination.with_suffix(destination.suffix + ".sha256").write_text(digest + "\n", encoding="ascii")
    print(json.dumps({"archive": str(destination), "sha256": digest, "files": len(inventory)}))


def verify(archive_path: Path):
    expected = archive_path.with_suffix(archive_path.suffix + ".sha256").read_text().strip()
    if sha(archive_path.read_bytes()) != expected:
        raise ValueError("Archive SHA-256 mismatch")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read(MANIFEST))
        names = [item["path"] for item in manifest["files"]]
        if len(names) != len(set(names)) or set(archive.namelist()) != set(names) | {MANIFEST}:
            raise ValueError("Duplicate or unlisted archive members")
        for item in manifest["files"]:
            path = PurePosixPath(item["path"])
            if (path.is_absolute() or ".." in path.parts or "\\" in item["path"]
                    or ":" in item["path"] or (path.parts[0] not in {"platform", ".github"} and item["path"]!="AGENTS.md")):
                raise ValueError("Unsafe archive path")
            data = archive.read(item["path"])
            if len(data) != item["bytes"] or sha(data) != item["sha256"]:
                raise ValueError("File hash mismatch: " + item["path"])
    return manifest


def restore(archive_path: Path, destination: Path):
    manifest = verify(archive_path)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        for item in manifest["files"]:
            output = destination / item["path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(item["path"]))
        (destination / MANIFEST).write_bytes(archive.read(MANIFEST))
    print(json.dumps({"restored": str(destination), "files": len(manifest["files"])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify", "restore"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.operation == "create":
        create(args.archive)
    elif args.operation == "verify":
        print("BASELINE_OK " + str(len(verify(args.archive)["files"])))
    else:
        if args.destination is None:
            parser.error("restore requires --destination (must not exist)")
        restore(args.archive, args.destination)


if __name__ == "__main__":
    main()
