"""Compare native and source-library fields using the same frozen OpenFOAM case."""

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
RUNNER = HERE.parents[1] / "server" / "app" / "services" / "openfoam_source_runner.py"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def execute(command, log, timeout):
    with log.open("wb") as stream:
        subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=timeout)


def times(root):
    result = {}
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            value = Decimal(entry.name)
        except InvalidOperation:
            continue
        if value.is_finite() and value>0:
            result[value] = entry
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case",type=Path,required=True)
    parser.add_argument("--library",type=Path,required=True)
    parser.add_argument("--solver",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--prepared-mesh",action="store_true",help="Use the mesh included in the input case")
    parser.add_argument("--timeout",type=float,default=120,help="Maximum seconds per solve in this verification")
    parser.add_argument("fields",nargs="+")
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    executable = shutil.which(args.solver)
    block_mesh = shutil.which("blockMesh")
    strace = shutil.which("strace")
    if not (executable and strace and (block_mesh or args.prepared_mesh)):
        parser.error("OpenFOAM and strace must be available in the loaded environment")
    library = args.library.resolve(strict=True)
    trace = args.output / "library-execve.log"
    with tempfile.TemporaryDirectory(prefix="feacraft-openfoam-parity-") as temporary:
        root = Path(temporary)
        frozen = root / "frozen"
        shutil.copytree(args.case,frozen)
        if times(frozen):
            raise ValueError("Use a case with no existing result times")
        if not args.prepared_mesh:
            execute([block_mesh,"-case",str(frozen)],args.output / "blockMesh.log",args.timeout)
        baseline = root / "baseline"
        source = root / "source"
        shutil.copytree(frozen,baseline)
        shutil.copytree(frozen,source)
        execute([executable,"-case",str(baseline)],args.output / "original.log",args.timeout)
        execute([strace,"-f","-e","trace=execve","-o",str(trace),sys.executable,"-I","-S",str(RUNNER),
                 "--library",str(library),"--solver",args.solver,"--","-case",str(source)],
                args.output / "source.log",args.timeout)
        original_times = times(baseline)
        source_times = times(source)
        assert original_times and original_times.keys()==source_times.keys()
        matching = {}
        for value in sorted(original_times):
            for field in args.fields:
                a = original_times[value] / field
                b = source_times[value] / field
                assert a.stat().st_size>0 and sha(a)==sha(b), f"Field mismatch at {value}/{field}"
                matching[f"{value}/{field}"] = sha(a)
    calls = re.findall(r'execve\("([^"]+)"',trace.read_text(encoding="utf8",errors="replace"))
    assert calls==[sys.executable], calls
    receipt = {
        "solver":args.solver, "openfoam":os.environ["WM_PROJECT_VERSION"],
        "original_executable_sha256":sha(Path(executable)),
        "source_library_sha256":sha(library), "written_times":len(original_times),
        "matching_field_sha256":matching, "source_execve":calls,
    }
    (args.output / "parity.json").write_text(json.dumps(receipt,indent=2)+"\n",encoding="utf8")
    print(f"PASS {args.solver}: {len(matching)} equivalent fields over {len(original_times)} write times")


if __name__ == "__main__":
    main()
