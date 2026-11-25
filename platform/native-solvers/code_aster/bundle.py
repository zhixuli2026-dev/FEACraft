"""Assemble a source-built Code_Aster runtime with its existing Conda dependencies."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile


SOURCE_SHA256 = "0a8d22aec1d242df7014fe3f9536221f1ac70b8a0fa4c03155eca33a62db8cc1"
REVISION = "87f699afe0706d84fd180e7a84d88bd0556e669c"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bundle(compiled, dependencies, output):
    metadata, = sorted((dependencies / "conda-meta").glob("code-aster-*.json"))
    original = json.loads(metadata.read_text(encoding="utf-8"))
    if original["name"] != "code-aster":
        raise ValueError("Expected the inventory of the dependency runtime's Code_Aster package")
    replaced = set(original["files"]) | {metadata.relative_to(dependencies).as_posix()}

    def exclude_original(directory, names):
        parent = Path(directory).relative_to(dependencies)
        return [name for name in names if (parent / name).as_posix() in replaced]

    required = ("bin/run_aster", "lib/libbibfor.so", "lib/python3.12/site-packages/libaster.so")
    for path in required:
        if not (compiled / path).is_file():
            raise ValueError(f"Source install is missing {path}")
    source_files = [path.relative_to(compiled) for path in sorted(compiled.rglob("*")) if path.is_file()]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feacraft-code-aster-", dir=output.parent) as directory:
        staged = Path(directory) / "runtime"
        shutil.copytree(dependencies, staged, symlinks=True, ignore=exclude_original)
        shutil.copytree(compiled, staged, symlinks=True, dirs_exist_ok=True)
        # Waf records the build interpreter's bin directory. Resolve Python
        # inside this installed runtime so the launcher also works in chroot.
        profile = staged / "share/aster/profile.sh"
        relocated, changed = re.subn(r"(?m)^export PATH=[^\n]+/bin:\$\{PATH\}$",
                                     'export PATH="${RUNASTER_ROOT}/bin:${PATH}"',
                                     profile.read_text(encoding="utf-8"))
        if changed != 1:
            raise ValueError("Unexpected Code_Aster interpreter search path")
        profile.write_text(relocated, encoding="utf-8")
        identity = {
            "schema": "feacraft.code-aster-source.v1",
            "version": "18.1.11",
            "revision": REVISION,
            "source_sha256": SOURCE_SHA256,
            "files": {path.as_posix(): digest(staged / path) for path in source_files},
        }
        (staged / "feacraft-code-aster-source.json").write_text(
            json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staged.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiled", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory")
    bundle(args.compiled.resolve(strict=True), args.dependencies.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
