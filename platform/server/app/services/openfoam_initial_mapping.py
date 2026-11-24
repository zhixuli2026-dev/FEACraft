"""Transfer finite-volume initial fields through explicitly verified native cell indices."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


_INDEX = 'vibeflowNativeCellIndex'


def _mesh(root, vtk, vtk_to_numpy):
    import numpy as np
    from .mesh_artifacts import boundaries
    from .node_storage_budget import reserve
    from .openfoam_boundary_inputs import field_dictionary

    marker = root/'mesh.foam'
    marker.touch()
    initial = root/'0'
    initial.mkdir()

    def read(use_index):
        reader = vtk.vtkOpenFOAMReader()
        errors = []
        reader.AddObserver('ErrorEvent', lambda *_: errors.append('read'))
        reader.SetFileName(str(marker))
        reader.CreateCellToPointOff()
        reader.SkipZeroTimeOff()
        reader.UpdateInformation()
        reader.DisableAllPatchArrays()
        reader.SetPatchArrayStatus('internalMesh',1)
        if use_index:
            reader.SetCellArrayStatus(_INDEX,1)
        reader.Update()
        output = reader.GetOutput()
        cells = output.GetBlock(0) if output.GetNumberOfBlocks() else None
        if errors or cells is None or not cells.IsA('vtkUnstructuredGrid') or not cells.GetNumberOfCells():
            raise ValueError('无法读取用于映射初值的原生网格')
        return cells

    count = read(False).GetNumberOfCells()
    if count > 2**24:
        raise ValueError('原生单元索引超过读取器可精确表达的范围')
    index = np.arange(count, dtype=np.int64)
    labels = 'nonuniform List<scalar> '+str(count)+'\n(\n'+'\n'.join(map(str,range(count)))+'\n)'
    conditions = {patch['name']:'type calculated; value uniform 0;' for patch in
                  boundaries(root/'constant/polyMesh/boundary')}
    # Use a valid dimensionless OpenFOAM scalar field to recover native addresses.
    index_path = initial/_INDEX
    generated = field_dictionary(_INDEX,'volScalarField','0 0 0 0 0 0 0',labels,conditions)
    with reserve(len(generated),target_root=root):
        try:
            index_path.write_bytes(generated)
            cells = read(True)
        finally:
            index_path.unlink(missing_ok=True)
    identifiers = cells.GetCellData().GetArray(_INDEX)
    if identifiers is None:
        raise ValueError('无法取得可核对的 OpenFOAM 单元编号')
    values = vtk_to_numpy(identifiers)
    if not np.array_equal(np.sort(values),index):
        raise ValueError('体网格不能按原生单元编号无损映射')
    return cells,values.astype(np.int64)


@contextmanager
def _mesh_scratch(prefix, size):
    from .. import storage
    from .node_storage_budget import reserve
    case = storage._fs_path(prefix,'.')
    scratch = case/'.vibeflow'
    scratch.mkdir(parents=True,exist_ok=True)
    from .attempt_workspace import ordinary
    ordinary(scratch)
    with reserve(size, target_root=scratch), TemporaryDirectory(dir=scratch,prefix='.field-map-') as folder:
        yield Path(folder)


def _render_sampled(field, data):
    import numpy as np
    if not np.all(np.isfinite(data)) or (field == 'T' and np.any(data <= 0)):
        raise ValueError('初值场含有无效物理量：'+field)
    if field == 'U':
        kind = 'vector'
        rows = ('('+' '.join(format(value,'.17g') for value in row)+')' for row in data)
    else:
        kind = 'scalar'
        rows = (format(value,'.17g') for value in data[:,0])
    return f'nonuniform List<{kind}> {len(data)}\n(\n'+'\n'.join(rows)+'\n)'


def map_fields(prefix, copy_source, source_run, copy_target, target_run, size, values):
    import numpy as np
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from .postprocess_executor import _parse_reference_field
    from .postprocess_reference_grid import sample
    from .result_readers import ResultReaderError

    with _mesh_scratch(prefix,size) as root:
        copy_source(root/'source')
        copy_target(root/'target')
        source,source_order = _mesh(root/'source'/source_run,vtk,vtk_to_numpy)
        target,target_order = _mesh(root/'target'/target_run,vtk,vtk_to_numpy)
        to_target = np.argsort(target_order)
        result = {}
        for field,internal in values.items():
            data = _parse_reference_field('internalField '+internal+';')
            if internal.startswith('uniform '):
                data = np.repeat(data,len(source_order),axis=0)
            if len(data) != len(source_order):
                raise ValueError('源初值单元列表与已核验网格不一致：'+field)
            array = numpy_to_vtk(data[source_order],deep=True)
            array.SetName(field)
            source.GetCellData().AddArray(array)
            try:
                sampled = sample(vtk,source,target,field)[to_target]
            except ResultReaderError as error:
                raise ValueError(str(error)) from error
            result[field] = _render_sampled(field,sampled)
        return result, {'method':'cellCenters','source_cells':len(source_order),'target_cells':len(target_order),
                        'covered_target_cells':len(target_order)}


def target_positions(prefix, copy_target, target_run, size):
    import numpy as np
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy

    with _mesh_scratch(prefix,size) as root:
        copy_target(root/'target')
        mesh,native_indices = _mesh(root/'target'/target_run,vtk,vtk_to_numpy)
        centers = vtk.vtkCellCenters()
        centers.SetInputData(mesh)
        centers.CopyArraysOff()
        centers.Update()
        points = centers.GetOutput().GetPoints()
        if points is None or points.GetNumberOfPoints() != len(native_indices):
            raise ValueError('无法取得生成初值需要的每个单元中心')
        return vtk_to_numpy(points.GetData())[np.argsort(native_indices)].astype(float)


def analytical_fields(positions, formulas):
    import numpy as np

    values = {}
    for field,formula in formulas.items():
        axis = 'xyz'.index(formula['axis'])
        displacement = positions[:,[axis]]-formula['origin_m']
        with np.errstate(over='ignore',invalid='ignore'):
            data = np.asarray(formula['value']) + displacement*np.asarray(formula['slope_per_m'])
        values[field] = _render_sampled(field,data)
    return values
