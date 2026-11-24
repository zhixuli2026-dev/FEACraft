"""Verify the particle identity and fields of a DualSPHysics dam-break run."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re

import numpy as np
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkIOLegacy import vtkDataSetReader


def snapshot(path):
    reader = vtkDataSetReader()
    reader.SetFileName(str(path))
    reader.ReadAllScalarsOn()
    reader.ReadAllVectorsOn()
    reader.ReadAllFieldsOn()
    reader.Update()
    grid = reader.GetOutput()
    points = vtk_to_numpy(grid.GetPoints().GetData()).copy()
    fields = grid.GetPointData()
    ids, velocity, density, pressure = (vtk_to_numpy(fields.GetArray(name)).copy()
                                        for name in ('Idp', 'Vel', 'Rhop', 'Press'))
    if len(points) == 0 or len(np.unique(ids)) != len(points):
        raise ValueError(f'Missing or duplicated particle IDs in {path}')
    if not all(np.isfinite(values).all() for values in (points, velocity, density, pressure)):
        raise ValueError(f'Found nonfinite particle data in {path}')
    if not np.allclose(points[:, 1], 0) or not np.all((density > 700) & (density < 1300)):
        raise ValueError(f'Particle data outside the 2-D test bounds in {path}')
    details = {'particles': len(points), 'front_x_m': float(points[:, 0].max()),
               'max_speed_m_s': float(np.linalg.norm(velocity, axis=1).max()),
               'density_range_kg_m3': [float(density.min()), float(density.max())],
               'pressure_range_pa': [float(pressure.min()), float(pressure.max())],
               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return details, ids, points


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='parent of the continuation outputs')
    root = parser.parse_args().directory
    checkpoint_frame, final_frame = map(int, (root / 'frames.txt').read_text().splitlines())
    with (root / 'continuation' / 'RunPARTs.csv').open(newline='') as source:
        parts = list(csv.DictReader((line for line in source if line.strip() and not line.startswith('#')),
                                    delimiter=';'))
    times = [float(row['TimeStep [s]']) for row in parts]
    log = (root / 'resume.log').read_text(encoding='utf-8')
    terminal = re.search(rf'^{final_frame:05d}\s+([0-9.]+)\s', log, re.MULTILINE)
    final_time = float(terminal.group(1)) if terminal else 0
    if (not times or int(parts[-1]['Part']) != final_frame or not 2 <= times[-1] < 2.001
            or abs(times[-1] - final_time) > 1e-6 or any(a >= b for a, b in zip(times, times[1:]))
            or 'Finished execution (code=0).' not in log):
        raise ValueError('The physical timeline did not reach the two-second target')
    initial, initial_ids, _ = snapshot(root / 'continuation' / 'FirstFluid_0000.vtk')
    checkpoint, checkpoint_ids, checkpoint_points = snapshot(
        root / 'continuation' / f'Checkpoint_{checkpoint_frame:04d}.vtk')
    restart, restart_ids, restart_points = snapshot(
        root / 'continuation' / f'Restart_{checkpoint_frame:04d}.vtk')
    last, last_ids, _ = snapshot(root / 'continuation' / f'LastFluid_{final_frame:04d}.vtk')
    if initial['particles'] != 20_000 or last['particles'] < 19_000 or len(np.setdiff1d(last_ids, initial_ids)):
        raise ValueError('The final output added unexplained fluid particle identities')
    checkpoint_order, restart_order = np.argsort(checkpoint_ids), np.argsort(restart_ids)
    if not np.array_equal(checkpoint_ids[checkpoint_order], restart_ids[restart_order]):
        raise ValueError('Particle IDs changed on reading the checkpoint')
    restart_error = float(np.abs(checkpoint_points[checkpoint_order] - restart_points[restart_order]).max())
    if restart_error > 1e-5 or last['front_x_m'] <= initial['front_x_m'] + 1 or last['max_speed_m_s'] < .1:
        raise ValueError('Checkpoint continuity or the dam-break motion was invalid')
    print(json.dumps({'passed': True, 'checkpoint_frame': checkpoint_frame, 'final_frame': final_frame,
                      'restart_physical_seconds': times[0], 'final_physical_seconds': times[-1],
                      'restart_position_max_difference_m': restart_error, 'initial': initial,
                      'checkpoint': checkpoint, 'restart': restart, 'final': last}, indent=2))


if __name__ == '__main__':
    main()
