"""Verify a Code_Aster source runtime with the analytical axial-bar case."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import medcoupling as med
import numpy as np


FIXTURE = Path(__file__).resolve().parents[2] / "server" / "examples" / "code_aster" / "axial"


def axial(directory):
    text = (directory / "axial.mess").read_text(encoding="utf-8")
    tip = float(re.search(r"^VIBEFLOW_AXIAL_TIP_M=([0-9e.+-]+)$", text, re.M).group(1))
    np.testing.assert_allclose(tip, 1e-3, rtol=0., atol=1e-12)
    file = str(directory / "axial.rmed")
    mesh, = med.GetMeshNames(file)
    field, = med.GetAllFieldNames(file)
    (iteration, order), = med.GetFieldIterations(med.ON_NODES, file, mesh, field)
    values = med.ReadFieldNode(file, mesh, 0, field, iteration, order).getArray().toNumPyArray().copy()
    np.testing.assert_allclose(np.sort(values[:, 0]), [0., 1e-3], rtol=0., atol=1e-12)
    np.testing.assert_allclose(values[:, 1:], 0., rtol=0., atol=1e-12)
    return tip, values


def solve(prefix, work, evidence):
    work.mkdir()
    evidence.mkdir()
    for name in ("axial.export", "axial.comm", "axial.mail"):
        shutil.copyfile(FIXTURE / name, work / name)
    env = dict(os.environ, PATH=f"{prefix}/bin:{os.defpath}", LD_LIBRARY_PATH=f"{prefix}/lib",
               PYTHONPATH=f"{prefix}/lib/python3.12/site-packages", PYTHONNOUSERSITE="1",
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
    with (evidence / "solver.log").open("wb") as log:
        subprocess.run(["strace", "-f", "-e", "trace=execve,openat", "-o", str(evidence / "native.trace"),
                        str(prefix / "bin/run_aster"), "axial.export"], cwd=work, stdout=log,
                       stderr=subprocess.STDOUT, env=env, timeout=120, check=True)
    for name in ("axial.mess", "axial.rmed"):
        shutil.copyfile(work / name, evidence / name)
    return axial(evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Source-built runtime prefix")
    parser.add_argument("--reference", type=Path, required=True, help="Existing Code_Aster 18.1.11 runtime prefix")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, original, output = args.source.resolve(strict=True), args.reference.resolve(strict=True), args.output.resolve()
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="feacraft-aster-parity-") as directory:
        root = Path(directory)
        tip, field = solve(source, root / "source", output / "source")
        ref_tip, ref_field = solve(original, root / "original", output / "original")
    np.testing.assert_allclose(field, ref_field, rtol=0., atol=1e-12)
    trace = (output / "source" / "native.trace").read_text(encoding="utf-8").splitlines()
    assert any(f'execve("{source}/bin/python3"' in line for line in trace)
    for library in ("libaster.so", "libbibfor.so"):
        assert any(f'"{source}/' in line and library in line and "O_RDONLY" in line
                   and line.partition(" = ")[2].isdigit() for line in trace)
    result = {"source_tip_m": tip, "original_tip_m": ref_tip, "nodes": len(field),
              "maximum_med_displacement_difference_m": float(np.max(np.abs(field - ref_field)))}
    (output / "parity.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("ASTER_SOURCE_PARITY_OK", output, flush=True)


if __name__ == "__main__":
    main()
