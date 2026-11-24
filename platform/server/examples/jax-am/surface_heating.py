"""Exercise JAX-AM's native moving surface heat source and VTU output on a CPU."""
import json
from pathlib import Path

import jax
import jax.numpy as jnp
import meshio
import numpy as np
from jax_am import __version__
from jax_am.cfd.cfd_am import AM_3d, mesh3d
from jax_am.common import box_mesh


def solve(root, power):
    root.mkdir()
    size = [1e-3, 2e-4, 1e-4]
    cells = [20, 8, 4]
    grid = mesh3d(size, cells)
    dt = 5e-6
    args = {
        "mesh": grid, "mesh_local": grid, "meshio_mesh": box_mesh(*cells, *size),
        "rho": 8440, "visco": 7e-3, "Marangoni": -2e-5,
        "dt": dt, "t_OFF": 2e-4, "speed": 0.5, "X0": [2e-4, 1e-4, 1e-4],
        "rb": 5e-5, "eta": 0.43, "P": power, "T_ref": 300, "Ts": 1563, "Tl": 1623,
        "latent_heat": 270000, "heat_source": 0, "write_sol_interval": 8, "data_dir": str(root),
        "cp": lambda temp: 0.2441 * jnp.clip(temp, 300, 1563) + 338.39,
        "k": lambda temp: 0.0163105 * jnp.clip(temp, 300, 1563) + 4.5847,
    }
    solver = AM_3d(args)
    solver.write_sols(0)
    peaks = [300.0]
    for step in range(1, 17):
        solver.time_integration()
        if step in (8, 16):
            solver.write_sols(step)
            peaks.append(float(jnp.max(solver.T)))

    native = np.asarray(solver.T).reshape(-1)
    final = meshio.read(root / "vtk/cfd/sols/u002.vtu")
    saved = final.cell_data_dict["T"]["hexahedron"].reshape(-1)
    assert len(saved) == len(native) == 640
    assert np.array_equal(saved, native.astype(np.float32))
    assert np.isfinite(native).all() and np.isfinite(np.asarray(solver.vel)).all()
    assert abs(solver.t - 16 * dt) <= 1e-12
    return native, peaks, solver.t


def main():
    out = Path("jax-am-result").resolve()
    out.mkdir()
    control, control_peaks, _ = solve(out / "zero", power=0)
    heated, heated_peaks, time = solve(out / "heated", power=20)
    assert np.max(np.abs(control - 300)) < 1e-5
    assert heated_peaks[2] > heated_peaks[1] > heated_peaks[0] + 1
    assert heated.mean() > control.mean()
    result = {"jax_am": __version__, "jax": jax.__version__, "backend": jax.default_backend(),
              "cells": len(heated), "end_time_s": time, "no_source_max_k": control_peaks,
              "heated_max_k": heated_peaks, "heated_mean_k": float(heated.mean()),
              "vtu_temperature_matches_float32": True}
    (out / "heat.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("JAX_AM_SURFACE_HEATING_OK", json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
