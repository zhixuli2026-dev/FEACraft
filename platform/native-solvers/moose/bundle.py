"""Stage a freshly linked MOOSE ELF and its application libraries for runtime."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def linked_libraries(entry):
    output = subprocess.check_output(["ldd", str(entry)], text=True)
    if "=> not found" in output:
        raise ValueError(f"Unresolved dependencies for {entry}\n{output}")
    return {
        name: Path(path).resolve(strict=True)
        for name, path in re.findall(r"(?m)^\s*(\S+) => (.+?) \(0x[0-9a-f]+\)$", output)
    }


def bundle(source, entry, dependencies, output):
    if not entry.is_relative_to(source):
        raise ValueError("Select an executable built inside the MOOSE source tree")
    if output.exists() or output == Path("/tmp") or output.is_relative_to("/tmp"):
        raise ValueError("Use a new runtime directory outside /tmp")
    if os.environ.get("LD_PRELOAD") or os.environ.get("LD_AUDIT"):
        raise ValueError("Clear LD_PRELOAD and LD_AUDIT before staging the runtime")
    libraries = {name: path for name, path in linked_libraries(entry).items() if path.is_relative_to(source)}
    if "libmoose-opt.so.0" not in libraries:
        raise ValueError("The executable must link the freshly built MOOSE framework")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feacraft-moose-", dir=output.parent) as temporary:
        root = Path(temporary) / "runtime"
        (root / "bin").mkdir(parents=True)
        (root / "lib").mkdir()
        executable = root / "bin" / entry.name
        shutil.copy2(entry, executable)
        for name, library in libraries.items():
            if Path(name).name != name:
                raise ValueError(f"Invalid dependency name: {name}")
            shutil.copy2(library, root / "lib" / name)
        shutil.copytree(source / "framework/data", root / "share/moose/data")
        if entry.name == "moose_test-opt":
            shutil.copytree(source / "test/data", root / "share/moose_test/data")
        if entry.name == "combined-opt":
            for data in sorted(source.glob("modules/*/data")):
                shutil.copytree(data, root / "share" / data.parent.name / "data")
        for file, origin in [
            (executable, "$ORIGIN/../lib"),
            *((root / "lib" / name, "$ORIGIN") for name in sorted(libraries)),
        ]:
            subprocess.run(["patchelf", "--set-rpath", f"{origin}:{dependencies / 'lib'}", file], check=True)
        loaded = linked_libraries(executable)
        if any(loaded.get(name) != root / "lib" / name for name in libraries):
            raise ValueError("MOOSE runtime resolved an application library outside the staged bundle")
        if "libmpi.so.12" not in loaded or any(path.is_relative_to("/tmp") for path in loaded.values()):
            raise ValueError("MOOSE requires the configured MPICH and runtime libraries outside /tmp")
        subprocess.run([executable, "--version"], check=True)
        if entry.name == "combined-opt":
            (root / "bin" / "moose-opt").symlink_to(entry.name)
        with (root / "SHA256SUMS").open("w", encoding="ascii") as manifest:
            for file in [executable, *sorted((root / "lib").iterdir()),
                         *(path for path in sorted((root / "share").rglob("*")) if path.is_file())]:
                with file.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                manifest.write(f"{digest}  {file.relative_to(root)}\n")
        root.rename(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("entry", type=Path)
    parser.add_argument("dependencies", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    bundle(source, (source / args.entry).resolve(strict=True), args.dependencies.resolve(strict=True),
           args.output.resolve())


if __name__ == "__main__":
    main()
