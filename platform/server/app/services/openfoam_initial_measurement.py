"""Read same-case initial samples with explicit units and target coverage."""

import csv
import hashlib
from io import StringIO

from .attempt_workspace import checked_path
from .case_boundary_inputs import UNITS, measurement_columns, measurement_source, scalar
from .openfoam_boundary_inputs import _PHYSICAL_TYPES, _quantity_transform, source_fields


_MAX_BYTES = 16*1024*1024


def prepare(root,run_dir,cfg,parsed,initial):
    relative,coordinate_unit,distance = measurement_source(initial.get('source'))
    path = run_dir+'/'+relative
    with checked_path(root,path).open('rb') as stream:
        data = stream.read(_MAX_BYTES+1)
    if not data or len(data) > _MAX_BYTES:
        raise ValueError('测量初值 CSV 需要非空内容且不得超过 16 MiB')
    specs = {}
    for field in source_fields(cfg,initial):
        entry = initial['fields'][field]
        columns = measurement_columns(field,entry)
        unit = entry.get('unit')
        kind = _PHYSICAL_TYPES[field]
        if not isinstance(unit,str) or unit not in UNITS[kind]:
            raise ValueError('测量初值单位与字段不匹配：'+field)
        specs[field] = (columns,unit,_quantity_transform(kind,unit,parsed,field=field))
    reader = csv.DictReader(StringIO(data.decode('utf-8-sig'),newline=''),strict=True)
    try:
        header = reader.fieldnames or []
        required = {'x','y','z'}.union(*(columns for columns,_,_ in specs.values()))
        if not required <= set(header) or len(set(header)) != len(header):
            raise ValueError('测量初值 CSV 缺列或列名重复')
        coordinates = []
        seen = set()
        samples = {field:[] for field in specs}
        factor = .001 if coordinate_unit == 'mm' else 1.
        for row in reader:
            if None in row or any(row[name] is None for name in required):
                raise ValueError('测量初值 CSV 行与表头列数不一致')
            position = tuple(scalar(row[axis])*factor for axis in 'xyz')
            if position in seen:
                raise ValueError('测量初值 CSV 存在重复坐标')
            seen.add(position)
            coordinates.append(position)
            for field,(columns,_,convert) in specs.items():
                values = tuple(scalar(row[name]) for name in columns)
                converted = convert(values if field == 'U' else values[0])
                samples[field].append(converted if field == 'U' else (converted,))
    except csv.Error as error:
        raise ValueError('测量初值 CSV 格式不正确') from error
    if not coordinates:
        raise ValueError('测量初值 CSV 没有数据行')
    proof = {'path':path,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'coordinate_unit':coordinate_unit,
             'maximum_distance_m':distance,'fields':{field:{'columns':columns,'unit':unit}
                                            for field,(columns,unit,_) in specs.items()}}
    return coordinates,samples,proof


def select(positions,coordinates,samples,measurement):
    import numpy as np
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk
    from .openfoam_initial_mapping import _render_sampled

    coordinates = np.asarray(coordinates)
    points = vtk.vtkPoints()
    points.SetData(numpy_to_vtk(coordinates,deep=True))
    cloud = vtk.vtkPolyData()
    cloud.SetPoints(points)
    index = vtk.vtkStaticPointLocator()
    index.SetDataSet(cloud)
    index.BuildLocator()
    closest = np.asarray([index.FindClosestPoint(position) for position in positions])
    if len(closest) == 0 or np.any(closest < 0):
        raise ValueError('无法选择覆盖目标单元的测量初值样本')
    distances = np.linalg.norm(positions-coordinates[closest],axis=1)
    failures = int(np.count_nonzero(distances > measurement['maximum_distance_m']))
    if failures:
        raise ValueError(f'{failures}/{len(positions)} 个单元中心超出有效测量距离')
    values = {field:_render_sampled(field,np.asarray(data)[closest]) for field,data in samples.items()}
    proof = {'method':'nearestSample','samples':len(coordinates),'cells':len(positions),
             'maximum_observed_distance_m':float(np.max(distances)),'measurement':measurement}
    return values,proof
