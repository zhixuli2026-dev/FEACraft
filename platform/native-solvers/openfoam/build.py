"""Build OpenFOAM application sources as separately loadable libraries."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
VERSION = "v2406"
APPLICATIONS = {
    "acousticFoam": "acoustic/acousticFoam",
    "icoFoam": "incompressible/icoFoam",
    "laplacianFoam": "basic/laplacianFoam",
    "overLaplacianDyMFoam": "basic/laplacianFoam/overLaplacianDyMFoam",
    "simpleFoam": "incompressible/simpleFoam",
    "pimpleFoam": "incompressible/pimpleFoam",
    "rhoCentralFoam": "compressible/rhoCentralFoam",
    "interFoam": "multiphase/interFoam",
    "buoyantBoussinesqSimpleFoam": "heatTransfer/buoyantBoussinesqSimpleFoam",
    "chtMultiRegionFoam": "heatTransfer/chtMultiRegionFoam",
    "overPimpleDyMFoam": "incompressible/pimpleFoam/overPimpleDyMFoam",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_application(source_root, source, workspace):
    # Preserve the upstream relative include layout and copy the selected
    # application so wmake never modifies the original source tree.
    relative = source.relative_to(source_root)
    upstream, staged = source_root, workspace
    for component in relative.parts:
        for neighbor in upstream.iterdir():
            if neighbor.name != component:
                (staged / neighbor.name).symlink_to(neighbor, target_is_directory=neighbor.is_dir())
        upstream = upstream / component
        staged = staged / component
        if upstream != source:
            staged.mkdir()
    shutil.copytree(source, staged)
    return staged


def build(solver, source_root, output, wmake):
    source = source_root / "applications" / "solvers" / APPLICATIONS[solver]
    entry = source / f"{solver}.C"
    options = source / "Make" / "options"
    library_name = f"libfeacraft_{solver}.so"
    destination = output / library_name

    with tempfile.TemporaryDirectory(prefix=f"feacraft-{solver}-") as directory:
        workspace = Path(directory)
        application = stage_application(source_root, source, workspace)
        shutil.copyfile(HERE / "entry.C", application / "feacraft_entry.C")
        make = application / "Make"
        source_list = (make / "files").read_text(encoding="utf-8")
        source_list, entries = re.subn(rf"(?m)^{re.escape(entry.name)}\s*$", "feacraft_entry.C", source_list)
        source_list, targets = re.subn(r"(?m)^EXE\s*=.*$", f"LIB = {workspace / library_name[:-3]}", source_list)
        if entries != 1 or targets != 1:
            raise ValueError(f"{solver} has an unexpected source list")
        (make / "files").write_text(source_list, encoding="utf-8")
        upstream_options = options.read_text(encoding="utf-8")
        library_options, replacements = re.subn(r"(?m)^EXE_LIBS\s*=", "LIB_LIBS =", upstream_options)
        if replacements != 1:
            raise ValueError(f"{solver} has an unexpected link configuration")
        # Use a relative include so build paths have no effect on code selection.
        definition = f'EXE_INC += -DFEACRAFT_OPENFOAM_APPLICATION=\\\"{solver}.C\\\"\n'
        (make / "options").write_text(library_options + "\n" + definition, encoding="utf-8")
        subprocess.run([wmake, "libso"], cwd=application, check=True)
        shutil.copyfile(workspace / library_name, destination)

    return {
        "solver":solver, "openfoam":VERSION, "abi":os.environ["WM_OPTIONS"],
        "application":entry.relative_to(source_root).as_posix(),
        "application_sha256":digest(entry), "options_sha256":digest(options),
        "entry_sha256":digest(HERE / "entry.C"), "library":library_name,
        "library_sha256":digest(destination),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root",type=Path,default=os.environ.get("WM_PROJECT_DIR"),required=not os.environ.get("WM_PROJECT_DIR"))
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("solvers",nargs="+",choices=tuple(APPLICATIONS))
    args = parser.parse_args()
    if os.environ.get("WM_PROJECT_VERSION") != VERSION or not os.environ.get("WM_OPTIONS"):
        parser.error(f"Load the configured OpenFOAM {VERSION} environment first")
    wmake = shutil.which("wmake")
    if wmake is None:
        parser.error("wmake is required to build a source library")
    if len(set(args.solvers)) != len(args.solvers):
        parser.error("List each solver once")
    source_root = args.source_root.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a fresh output directory for the requested library set")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".feacraft-openfoam-",dir=output.parent) as directory:
        staged = Path(directory) / "libraries"
        staged.mkdir()
        records = [build(solver, source_root, staged, wmake) for solver in args.solvers]
        manifest = staged / "openfoam-source-libraries.json"
        manifest.write_text(json.dumps(records,indent=2)+"\n",encoding="utf-8")
        staged.rename(output)


if __name__ == "__main__":
    main()
