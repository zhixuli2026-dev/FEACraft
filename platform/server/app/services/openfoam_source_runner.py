"""Call an OpenFOAM source library in a fresh worker process."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys


SOLVERS = frozenset({
    "acousticFoam",
    "buoyantBoussinesqSimpleFoam",
    "chtMultiRegionFoam",
    "icoFoam",
    "interFoam",
    "laplacianFoam",
    "overLaplacianDyMFoam",
    "pimpleFoam",
    "rhoCentralFoam",
    "simpleFoam",
})


def library_path(directory, solver):
    path = PurePosixPath(directory)
    if (solver not in SOLVERS or path == PurePosixPath("/") or path.root != "/"
            or ".." in path.parts or str(path) != directory
            or any(ord(character) < 32 for character in directory)):
        raise ValueError("Invalid OpenFOAM source library directory or solver")
    return str(path / f"libfeacraft_{solver}.so")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_record(directory, solver):
    library = Path(library_path(directory, solver))
    records = json.loads((library.parent / "openfoam-source-libraries.json").read_text(encoding="utf-8"))
    matches = [entry for entry in records if entry.get("solver") == solver]
    if len(matches) != 1:
        raise ValueError("OpenFOAM source build record is missing or ambiguous")
    record = matches[0]
    if (record.get("library") != library.name or record.get("openfoam") != os.environ.get("WM_PROJECT_VERSION")
            or record.get("abi") != os.environ.get("WM_OPTIONS")
            or not all(re.fullmatch("[0-9a-f]{64}", record.get(key, ""))
                       for key in ("application_sha256", "options_sha256", "entry_sha256", "library_sha256"))
            or digest(library) != record["library_sha256"]):
        raise ValueError("OpenFOAM source library differs from its build record or runtime ABI")
    return str(library), {**record, "loader_sha256": digest(Path(__file__))}


def command(library, solver, arguments):
    return [sys.executable, "-I", "-S", str(Path(__file__).resolve()),
            "--library", library, "--solver", solver, "--", *arguments]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library",type=Path,required=True)
    parser.add_argument("--solver",required=True)
    parser.add_argument("arguments",nargs=argparse.REMAINDER)
    args = parser.parse_args()
    library = args.library.resolve(strict=True)
    if library.name != f"libfeacraft_{args.solver}.so" or not library.is_file():
        parser.error("Library does not match the requested solver")
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    # OpenFOAM takes argc and a writable argv, including its original argv[0].
    storage = [ctypes.create_string_buffer(os.fsencode(arg)) for arg in (args.solver,*arguments)]
    argv = (ctypes.c_char_p * (len(storage)+1))()
    for index, arg in enumerate(storage):
        argv[index] = ctypes.cast(arg,ctypes.c_char_p)
    native = ctypes.CDLL(str(library),mode=ctypes.RTLD_GLOBAL)
    run = native.feacraft_openfoam_run
    run.argtypes = (ctypes.c_int,ctypes.POINTER(ctypes.c_char_p))
    run.restype = ctypes.c_int
    raise SystemExit(run(len(storage),argv))


if __name__ == "__main__":
    main()
