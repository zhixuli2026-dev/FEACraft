"""Read the native LaserbeamFoam Plate2D fields and both ray series."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from xml.etree import ElementTree

import numpy as np
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkIOLegacy import vtkPolyDataReader
from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader


def read_fields(frame):
    datasets = ElementTree.parse(frame).findall('.//DataSet')
    if len(datasets) != 1:
        raise ValueError(f'Expected one internal mesh in {frame}')
    path = frame.parent / datasets[0].attrib['file']
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    grid = reader.GetOutput()
    if grid.GetNumberOfCells() != 3200 or grid.GetNumberOfPoints() == 0:
        raise ValueError(f'Unexpected Plate2D mesh in {path}')
    fields = grid.GetCellData()
    if any(fields.GetArray(name) is None for name in ('T', 'U', 'alpha.metal')):
        raise ValueError(f'Incomplete Plate2D fields in {path}')
    temperature, velocity, metal = (vtk_to_numpy(fields.GetArray(name))
                                    for name in ('T', 'U', 'alpha.metal'))
    if (velocity.shape != (3200, 3) or metal.shape != (3200,) or temperature.shape != (3200,)
            or not all(np.isfinite(values).all() for values in (temperature, velocity, metal))
            or np.any((metal < -1e-6) | (metal > 1 + 1e-6))):
        raise ValueError(f'Invalid Plate2D field values in {path}')
    return {'cells': grid.GetNumberOfCells(), 'points': grid.GetNumberOfPoints(),
            'temperature_k': [float(temperature.min()), float(temperature.max())],
            'maximum_speed_m_s': float(np.linalg.norm(velocity, axis=1).max()),
            'metal_volume_fraction': [float(metal.min()), float(metal.max())],
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def read_rays(series):
    frames = json.loads(series.read_bytes())['files']
    if [row['time'] for row in frames] != [step / 1000 for step in range(1, 11)]:
        raise ValueError(f'Unexpected ray write times in {series}')
    observations = []
    for row in frames:
        path = series.parent / row['name']
        reader = vtkPolyDataReader()
        reader.SetFileName(str(path))
        reader.Update()
        rays = reader.GetOutput()
        points, lines = rays.GetNumberOfPoints(), rays.GetNumberOfLines()
        if points and not np.isfinite(vtk_to_numpy(rays.GetPoints().GetData())).all():
            raise ValueError(f'Nonfinite ray geometry in {path}')
        observations.append({'time_s': row['time'], 'points': points, 'lines': lines,
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    root = parser.parse_args().directory
    log = (root / 'solve.log').read_text(encoding='utf-8')
    times = re.findall(r'^Time = ([0-9.e+-]+)', log, re.MULTILINE)
    if not times or float(times[-1]) != .01 or not log.rstrip().endswith('\nEnd'):
        raise ValueError('The native solver did not finish at 0.01 s')
    series, = (root / 'VTK-fields').glob('*.vtm.series')
    frames = json.loads(series.read_bytes())['files']
    if [row['time'] for row in frames] != [0, .01]:
        raise ValueError('The expected initial and final field frames are missing')
    fields = [{'time_s': row['time'], **read_fields(series.parent / row['name'])} for row in frames]
    if (fields[0]['temperature_k'] != [300, 300] or fields[1]['temperature_k'][1] <= 300
            or fields[1]['maximum_speed_m_s'] <= 0):
        raise ValueError('The expected initial state and heated fluid motion were not observed')
    rays = {f'laser{index}': read_rays(root / 'VTKs' / f'rays_laser{index}.vtk.series')
            for index in range(2)}
    if not any(row['lines'] > 0 for series in rays.values() for row in series):
        raise ValueError('There were no ray segments in the recorded frames')
    print(json.dumps({'passed': True, 'steps': len(times), 'fields': fields, 'rays': rays,
                      'solver_sha256': (root / 'solver.sha256').read_text().split()[0],
                      'actual_model_calls': 0}, indent=2))


if __name__ == '__main__':
    main()
