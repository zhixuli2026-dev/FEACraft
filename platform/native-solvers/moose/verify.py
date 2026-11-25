"""Verify the source-built MOOSE framework with a steady diffusion solve."""

import argparse
import csv
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkIOExodus import vtkExodusIIReader


FIXTURE = Path(__file__).resolve().parents[2] / "server" / "examples" / "moose" / "thermal" / "line.i"


def fields(directory):
    with (directory / "line_out.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert float(rows[-1]["time"]) == 1.0
    midpoint = float(rows[-1]["midpoint"])
    np.testing.assert_allclose(midpoint, 50.0, rtol=0., atol=1e-9)

    reader = vtkExodusIIReader()
    reader.SetFileName(str(directory / "line_out.e"))
    reader.UpdateInformation()
    reader.SetPointResultArrayStatus("u", 1)
    reader.SetTimeStep(1)
    reader.Update()
    iterator = reader.GetOutput().NewIterator()
    iterator.InitTraversal()
    grid = iterator.GetCurrentDataObject()
    assert grid.GetNumberOfPoints() == 11
    x = vtk_to_numpy(grid.GetPoints().GetData())[:, 0]
    values = vtk_to_numpy(grid.GetPointData().GetArray("u")).copy()
    order = np.argsort(x)
    np.testing.assert_allclose(x[order], np.linspace(0, 1, 11), rtol=0., atol=np.finfo(x.dtype).eps)
    np.testing.assert_allclose(values[order], np.linspace(0, 100, 11), rtol=0., atol=1e-9)
    return midpoint, values[order]


def solve(directory, environment, executable):
    directory.mkdir(parents=True)
    shutil.copyfile(FIXTURE, directory / "line.i")
    command = [*environment, "strace", "-f", "-e", "trace=execve,openat", "-o", "native.trace",
               executable, "-i", "line.i", "--n-threads=1"]
    with (directory / "solver.log").open("wb") as log:
        subprocess.run(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True)
    return fields(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default="vibeflow-linux")
    parser.add_argument("--dependencies", default="/opt/vibeflow-ff-c08/moose")
    parser.add_argument("--source-entry", required=True, help="Linux path to the source-built MOOSE application")
    parser.add_argument("--source-library-dir", action="append", required=True,
                        help="Linux directory with source-built libraries, repeat for multiple directories")
    parser.add_argument("--reference", default="/opt/vibeflow-ff-c08/moose/bin/moose-opt")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    environment = ["wsl.exe", "-d", args.distro, "--user", "nobody", "--exec", "env",
                   "FI_PROVIDER=tcp", "MPICH_CH4_NETMOD=ofi", "OMP_NUM_THREADS=1", "OPENBLAS_NUM_THREADS=1"]
    output = args.output.resolve()
    source, original = output / "source", output / "original"
    dependency_lib = args.dependencies + "/lib"
    source_libs = ":".join([*args.source_library_dir, dependency_lib])
    midpoint, values = solve(source, [*environment, f"LD_LIBRARY_PATH={source_libs}"], args.source_entry)
    ref_midpoint, ref_values = solve(original, [*environment, f"LD_LIBRARY_PATH={dependency_lib}"], args.reference)
    np.testing.assert_allclose(values, ref_values, rtol=0., atol=1e-9)
    trace = (source / "native.trace").read_text(encoding="utf-8").splitlines()
    assert any("libmoose-opt.so" in line and "O_RDONLY" in line and line.partition(" = ")[2].isdigit()
               and any(directory + "/" in line for directory in args.source_library_dir) for line in trace)
    result = {"nodes": len(values), "source_midpoint": midpoint, "original_midpoint": ref_midpoint,
              "max_node_difference": float(np.max(np.abs(values - ref_values)))}
    (output / "parity.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("MOOSE_SOURCE_PARITY_OK", output, flush=True)


if __name__ == "__main__":
    main()
