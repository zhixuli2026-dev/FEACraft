"""Sample a sealed VTU reference field at the target grid's cell centers."""

import numpy as np

if __package__:
    from .result_readers import ResultReaderError, load_result_file
else:
    from result_readers import ResultReaderError, load_result_file


def load_grid(path, field, association):
    if path.suffix.lower() != '.vtu':
        raise ResultReaderError('异网格参考场需要单文件 VTU 体网格')
    if association not in ('cell', 'point'):
        raise ResultReaderError('referenceAssociation 必须为 cell 或 point')
    source = load_result_file(path).dataset
    point_data, cell_data = source.GetPointData(), source.GetCellData()
    selected, unused = (cell_data, point_data) if association == 'cell' else (point_data, cell_data)
    values = selected.GetArray(field)
    if values is None:
        raise ResultReaderError(f'参考网格缺少 {association} 场 {field}')
    expected_size = source.GetNumberOfCells() if association == 'cell' else source.GetNumberOfPoints()
    if values.GetNumberOfTuples() != expected_size:
        raise ResultReaderError(f'参考网格场 {field} 的数据与其网格大小不符')
    from vtk.util.numpy_support import vtk_to_numpy
    if not np.all(np.isfinite(vtk_to_numpy(values))):
        raise ResultReaderError(f'参考网格场 {field} 含有非有限值')
    unused.Initialize()
    return source


def sample(vtk, source, target, field):
    from vtk.util.numpy_support import vtk_to_numpy

    centers = vtk.vtkCellCenters()
    centers.SetInputData(target)
    centers.CopyArraysOff()
    centers.Update()
    locations = centers.GetOutput()
    count = target.GetNumberOfCells()
    if not count or locations.GetNumberOfPoints() != count:
        raise ResultReaderError('无法取得每个目标单元的中心')

    probe = vtk.vtkProbeFilter()
    probe.SetInputData(locations)
    probe.SetSourceData(source)
    mask_name = 'vtkValidPointMask' if field != 'vtkValidPointMask' else '_vibeflowValidPointMask'
    probe.SetValidPointMaskArrayName(mask_name)
    probe.Update()
    output = probe.GetOutput().GetPointData()
    mask, values = output.GetArray(mask_name), output.GetArray(field)
    if mask is None or values is None or values.GetNumberOfTuples() != count:
        raise ResultReaderError('无法核对异网格采样的场和覆盖范围')
    uncovered = int(np.count_nonzero(vtk_to_numpy(mask) != 1))
    if uncovered:
        raise ResultReaderError(f'{uncovered}/{count} 个目标单元中心在参考网格有效域外')
    return vtk_to_numpy(values).astype(float).reshape(count, -1)
