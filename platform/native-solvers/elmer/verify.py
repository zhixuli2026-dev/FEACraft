"""Check the Elmer library build against the existing solver and an axial reference."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

import numpy as np
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader


EXAMPLE = Path(__file__).resolve().parents[2] / "server" / "examples" / "elmer" / "axial"


def solve(directory, strain, command, env):
    shutil.copytree(EXAMPLE, directory)
    config = directory / "axial.sif"
    config.write_text(config.read_text(encoding="ascii").replace(
        "Displacement 1 = 0.001", f"Displacement 1 = {strain:g}"), encoding="ascii")
    with (directory / "solver.log").open("wb") as log:
        subprocess.run([*command, "axial.sif"], cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                       env=env, timeout=60, check=True)
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(directory / "brick" / "axial_t0001.vtu"))
    reader.Update()
    grid = reader.GetOutput()
    assert grid.GetNumberOfPoints() == 12
    points = vtk_to_numpy(grid.GetPoints().GetData()).copy()
    displacements = vtk_to_numpy(grid.GetPointData().GetArray("displacement")).copy()
    stresses = vtk_to_numpy(grid.GetPointData().GetArray("stress_xx")).copy()
    undeformed = points - displacements
    order = np.lexsort(tuple(np.round(undeformed, 12).T))
    x = undeformed[:, 0]
    expected = np.zeros_like(displacements)
    expected[:, 0] = x * strain
    assert set(np.round(x, 12)) == {0., .5, 1.}
    np.testing.assert_allclose(displacements, expected, rtol=0., atol=1e-12)
    np.testing.assert_allclose(stresses, 210e9 * strain, rtol=0., atol=1e-5)
    return displacements[order], stresses[order]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distro", default="vibeflow-linux")
    parser.add_argument("--source-entry", required=True, help="Linux path to the source-built ElmerSolver")
    parser.add_argument("--source-library-dir", required=True, help="Linux path to the source-built solver modules")
    parser.add_argument("--reference", type=Path, required=True, help="Installed Windows ElmerSolver executable")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = args.reference.resolve(strict=True)
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", ELMER_HOME=str(reference.parent.parent),
               ELMER_LIB=str(reference.parent.parent / "share" / "elmersolver" / "lib"))
    native = ["wsl.exe", "-d", args.distro, "--user", "nobody", "--exec", "env",
              f"ELMER_HOME={PurePosixPath(args.source_entry).parents[1]}",
              f"ELMER_LIB={args.source_library_dir}", "OMP_NUM_THREADS=1", "OPENBLAS_NUM_THREADS=1",
              "strace", "-f", "-e", "trace=execve,openat", "-o", "native.trace", args.source_entry]
    results = []
    for strain in (.001, .002):
        source = args.output.resolve() / f"source-{strain:g}"
        original = args.output.resolve() / f"original-{strain:g}"
        displacement, stress = solve(source, strain, native, None)
        ref_displacement, ref_stress = solve(original, strain, [str(reference)], env)
        np.testing.assert_allclose(displacement, ref_displacement, rtol=0., atol=1e-12)
        np.testing.assert_allclose(stress, ref_stress, rtol=0., atol=1e-5)
        lines = (source / "native.trace").read_text(encoding="utf-8").splitlines()
        execs = [line for line in lines if "execve(" in line]
        assert len(execs) == 1 and f'execve("{args.source_entry}"' in execs[0], execs
        for library in ("libelmersolver.so", "StressSolve.so"):
            assert any(library in line and "O_RDONLY" in line and line.partition(" = ")[2].isdigit() for line in lines)
        results.append({"strain": strain, "nodes": len(displacement), "stress_pa": 210e9 * strain,
                        "max_displacement_difference_m": float(np.max(np.abs(displacement - ref_displacement))),
                        "max_stress_difference_pa": float(np.max(np.abs(stress - ref_stress)))})
    (args.output / "parity.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("ELMER_SOURCE_PARITY_OK", args.output, flush=True)


if __name__ == "__main__":
    main()
