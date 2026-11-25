"""Compare the source-library solver with the installed Windows CalculiX 2.23."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))
from linux_calculix_acceptance import DECK, MATERIAL, displacements, reference


def run(directory, load, command):
    directory.mkdir(parents=True)
    (directory / "C-run").mkdir()
    (directory / "C-run" / "material.inp").write_text(MATERIAL, encoding="ascii")
    (directory / "model.inp").write_text(DECK.replace("-1000", str(-load)), encoding="ascii")
    with (directory / "solver.log").open("wb") as log:
        subprocess.run([*command, "-i", "model"], cwd=directory, stdout=log,
                       stderr=subprocess.STDOUT, timeout=60, check=True)
    return displacements((directory / "model.frd").read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default="vibeflow-linux")
    parser.add_argument("--source-entry", required=True, help="Linux path to the source-library job entry point")
    parser.add_argument("--reference", type=Path, required=True, help="Installed Windows CalculiX executable")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    results = []
    for load in (1000, 2000):
        source = args.output / f"source-{load}"
        original = args.output / f"original-{load}"
        command = ["wsl.exe", "-d", args.distro, "--user", "nobody", "--exec",
                   "strace", "-f", "-e", "trace=execve,openat", "-o", "native.trace", args.source_entry]
        library_result = run(source, load, command)
        reference_result = run(original, load, [str(args.reference.resolve(strict=True))])
        expected = reference(load)
        np.testing.assert_allclose(library_result, expected, rtol=5e-5, atol=1e-8)
        np.testing.assert_allclose(library_result, reference_result, rtol=5e-5, atol=1e-8)
        lines = (source / "native.trace").read_text(encoding="utf-8").splitlines()
        execs = [line for line in lines if "execve(" in line]
        assert len(execs) == 1 and f'execve("{args.source_entry}"' in execs[0], execs
        assert any("libfeacraft_calculix.so" in line and "O_RDONLY" in line
                   and line.partition(" = ")[2].isdigit() for line in lines)
        results.append({"load": load, "nodes": len(expected),
                        "absolute_error_vs_reference": float(np.max(np.abs(library_result - reference_result))),
                        "absolute_error_vs_stiffness": float(np.max(np.abs(library_result - expected)))})
    (args.output / "parity.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("CALCULIX_SOURCE_PARITY_OK", args.output, flush=True)


if __name__ == "__main__":
    main()
