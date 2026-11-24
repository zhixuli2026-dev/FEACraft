"""Read Nektar++ tutorial VTU outputs and verify convergence to its exact field."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader


def verify_run(directory, modes):
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(directory / "Helm_mesh.vtu"))
    reader.Update()
    grid = reader.GetOutput()
    points, cells = grid.GetNumberOfPoints(), grid.GetNumberOfCells()
    field = grid.GetPointData().GetArray("u")
    if not points or not cells or field is None or field.GetNumberOfTuples() != points:
        raise ValueError(f"Incomplete Helmholtz grid in {directory}")

    errors = []
    for index in range(points):
        x, y, _ = grid.GetPoint(index)
        expected = math.cos(math.pi * x) * math.cos(math.pi * y)
        error = abs(field.GetTuple1(index) - expected)
        if not math.isfinite(error):
            raise ValueError(f"Nonfinite Helmholtz value at point {index} in {directory}")
        errors.append(error)

    log = (directory / "solve.log").read_text(encoding="utf-8")
    l2, linf = (float(value) for value in re.findall(r"L (?:2|inf) error[^:]+: (\S+)", log))
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (directory / "Helm_mesh.fld", directory / "Helm_mesh.vtu")
    }
    return {"modes": modes, "points": points, "cells": cells, "solver_l2": l2,
            "solver_linf": linf, "point_rmse": math.sqrt(math.fsum(e * e for e in errors) / points),
            "point_max_error": max(errors), "sha256": hashes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="parent of modes-3, modes-5 and modes-7")
    args = parser.parse_args()
    runs = [verify_run(args.directory / f"modes-{modes}", modes) for modes in (3, 5, 7)]
    for metric in ("solver_l2", "solver_linf", "point_rmse", "point_max_error"):
        low, middle, high = (run[metric] for run in runs)
        if not (0 < high < middle < low and high < 1e-5):
            raise ValueError(f"Expected convergence in {metric}, got {low}, {middle}, {high}")
    print(json.dumps({"passed": True, "runs": runs}, indent=2))


if __name__ == "__main__":
    main()
