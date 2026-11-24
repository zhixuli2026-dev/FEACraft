"""Read SPHinXsys's 2-D dam-break snapshots and the two physical probes."""

import argparse
import hashlib
import json
from pathlib import Path
import re

import numpy as np
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkIOXML import vtkXMLPolyDataReader


def read_frame(path):
    reader = vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    data = reader.GetOutput()
    if data.GetNumberOfPoints() != 3200:
        raise ValueError(f'Unexpected water count in {path}')
    positions = vtk_to_numpy(data.GetPoints().GetData())
    fields = data.GetPointData()
    ids = vtk_to_numpy(fields.GetArray('OriginalID'))
    velocity = vtk_to_numpy(fields.GetArray('Velocity'))
    if (not np.array_equal(np.sort(ids), np.arange(3200)) or velocity.shape != (3200, 3)
            or not np.isfinite(positions).all() or not np.isfinite(velocity).all()
            or not np.allclose(positions[:, 2], 0)):
        raise ValueError(f'Invalid water identity, position or velocity in {path}')
    return {'particles': len(positions), 'front_x': float(positions[:, 0].max()),
            'maximum_speed': float(np.linalg.norm(velocity, axis=1).max()),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def read_probe(path, last_frame_time):
    data = np.loadtxt(path, skiprows=1)
    if (data.shape[1] != 2 or len(data) < 20 or not np.isfinite(data).all()
            or data[0, 0] != 0 or np.any(np.diff(data[:, 0]) <= 0)
            or not 19 <= data[-1, 0] <= last_frame_time or data[:, 1].max() <= 0):
        raise ValueError(f'Invalid dam-break probe in {path}')
    return {'samples': len(data), 'last_sample_time': float(data[-1, 0]),
            'first_value': float(data[0, 1]), 'last_value': float(data[-1, 1]),
            'maximum': float(data[:, 1].max()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='parent of the two recorded particle snapshots')
    root = parser.parse_args().directory
    log = (root / 'solve.log').read_text(encoding='utf-8')
    signals = {'Pressure': 'FluidObserver_Pressure.dat',
               'TotalMechanicalEnergy': 'WaterBody_TotalMechanicalEnergy.dat'}
    if not all(f'The result of {name} is correct based on the dynamic time warping regression test!' in log
               for name in signals):
        raise ValueError('Upstream pressure and energy regressions must both pass')
    paths = [name for name in (root / 'frames.txt').read_text().splitlines()
             if re.fullmatch(r'WaterBody_\d{10}\.vtp', name)]
    times = [int(Path(name).stem.split('_')[-1]) / 1e6 for name in paths]
    if (len(times) < 190 or times[0] != 0 or not 20 <= times[-1] < 20.2
            or any(a >= b for a, b in zip(times, times[1:]))):
        raise ValueError('Incorrect dam-break frame timeline')
    assets = root / 'output'
    first, last = read_frame(assets / paths[0]), read_frame(assets / paths[-1])
    if first['maximum_speed'] != 0 or not 5 < last['front_x'] < 5.4 or last['maximum_speed'] <= 0:
        raise ValueError('Missing expected dam-break travel and acceleration')
    print(json.dumps({'passed': True, 'water_frames': len(times), 'end_time': times[-1], 'initial': first,
                      'final': last, 'probes': {key: read_probe(assets / name, times[-1])
                                                for key, name in signals.items()},
                      'upstream_dtw_passed': list(signals), 'actual_model_calls': 0}, indent=2))


if __name__ == '__main__':
    main()
