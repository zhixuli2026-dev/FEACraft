"""Compare two actual SU2 NACA0012 solves at each mesh point."""

import argparse
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader


EXAMPLES = Path(__file__).resolve().parents[2] / "server" / "examples" / "su2"
FIELDS = ("Density", "Pressure", "Temperature", "Mach")


def solve(path, command):
    path.mkdir(parents=True)
    for name in ("inv_NACA0012.cfg", "mesh_NACA0012_inv.su2"):
        shutil.copyfile(EXAMPLES / name, path / name)
    env = dict(os.environ, OMP_NUM_THREADS="1")
    with (path / "solver.log").open("wb") as log:
        subprocess.run([*command, "inv_NACA0012.cfg"], cwd=path, stdout=log,
                       stderr=subprocess.STDOUT, env=env, timeout=180, check=True)
    with (path / "history.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, skipinitialspace=True))
    assert 1 < len(rows) <= 250
    residual = next(key for key in rows[0] if "rms" in key.lower() and "rho" in key.lower())
    first, last = float(rows[0][residual]), float(rows[-1][residual])
    assert math.isfinite(first) and math.isfinite(last) and last < first - 1
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path / "flow.vtu"))
    reader.Update()
    grid = reader.GetOutput()
    assert grid.GetNumberOfPoints() == 5233 and grid.GetNumberOfCells() == 10216
    values = {name: vtk_to_numpy(grid.GetPointData().GetArray(name)).copy() for name in FIELDS}
    coordinates = vtk_to_numpy(grid.GetPoints().GetData()).copy()
    return coordinates, values, {"first": first, "last": last, "iterations": len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default="vibeflow-linux")
    parser.add_argument("--source-entry", required=True, help="Linux path to the source-library job entry point")
    parser.add_argument("--reference", type=Path, required=True, help="Installed Windows SU2 8.5.0 executable")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.output.resolve() / "source"
    original = args.output.resolve() / "original"
    command = ["wsl.exe", "-d", args.distro, "--user", "nobody", "--exec", "env", "OMP_NUM_THREADS=1",
               "strace", "-f", "-e", "trace=execve,openat", "-o", "native.trace", args.source_entry]
    coordinates, values, residual = solve(source, command)
    ref_coordinates, ref_values, ref_residual = solve(original, [str(args.reference.resolve(strict=True))])
    np.testing.assert_array_equal(coordinates, ref_coordinates)
    errors = {}
    for name in FIELDS:
        np.testing.assert_allclose(values[name], ref_values[name], rtol=2e-6, atol=1e-10)
        errors[name] = float(np.max(np.abs(values[name] - ref_values[name])))
    lines = (source / "native.trace").read_text(encoding="utf-8").splitlines()
    execs = [line for line in lines if "execve(" in line]
    assert len(execs) == 1 and f'execve("{args.source_entry}"' in execs[0], execs
    assert any("libfeacraft_su2.so" in line and "O_RDONLY" in line
               and line.partition(" = ")[2].isdigit() for line in lines)
    result = {"mesh_points": len(coordinates), "source_density_residual": residual,
              "original_density_residual": ref_residual, "maximum_absolute_field_difference": errors}
    (args.output / "parity.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("SU2_SOURCE_PARITY_OK", args.output, flush=True)


if __name__ == "__main__":
    main()
