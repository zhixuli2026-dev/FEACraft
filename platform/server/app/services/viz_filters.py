"""Field selection and native pipelines for result-analysis filters."""
import math


class FilterInputError(ValueError):
    pass


def finite_number(value, label):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise FilterInputError(f"{label}需要有限数值") from error
    if not math.isfinite(number):
        raise FilterInputError(f"{label}需要有限数值")
    return number


def field_names(grid, components):
    names = {}
    for data in (grid.GetPointData(), grid.GetCellData()):
        for index in range(data.GetNumberOfArrays()):
            array = data.GetArray(index)
            if array is not None and array.GetName():
                names.setdefault(array.GetName(), array.GetNumberOfComponents())
    return [name for name, count in names.items() if count in components]


def vector_field(grid, name=None, *, deformation=False):
    names = field_names(grid, (3,))
    if name:
        if name not in names:
            raise FilterInputError(f"“{name}”需要三分量向量场")
        return name
    preferred = ("displacement", "Displacement", "D") if deformation else ("U",)
    for candidate in preferred:
        if candidate in names:
            return candidate
    active = grid.GetPointData().GetVectors()
    if not deformation and active is not None and active.GetName() in names:
        return active.GetName()
    if len(names) == 1:
        return names[0]
    raise FilterInputError("请选择三分量向量场" if names else "当前数据没有三分量向量场")


def point_field(vtk, grid, name, components, *, association="auto"):
    from numpy import isfinite
    from vtk.util.numpy_support import vtk_to_numpy

    if association not in ("auto", "point", "cell"):
        raise FilterInputError("场关联须为 auto、point 或 cell")
    if not isinstance(name, str) or not name:
        raise FilterInputError("请选择输入场")
    point = grid.GetPointData().GetArray(name) if association != "cell" else None
    if point is None and association != "point":
        cell = grid.GetCellData().GetArray(name)
        if cell is not None and cell.GetNumberOfComponents() in components:
            interpolator = vtk.vtkCellDataToPointData()
            interpolator.SetInputData(grid)
            interpolator.ProcessAllArraysOff()
            interpolator.AddCellDataArray(name)
            interpolator.PassCellDataOn()
            interpolator.Update()
            typed = grid.NewInstance()
            typed.ShallowCopy(interpolator.GetOutput())
            grid, point = typed, typed.GetPointData().GetArray(name)
    if (point is None or point.GetNumberOfComponents() not in components
            or point.GetNumberOfTuples() != grid.GetNumberOfPoints() or not isfinite(vtk_to_numpy(point)).all()):
        required = "或".join(str(count) for count in components)
        raise FilterInputError(f"场“{name}”缺失，或不是具有有限值的{required}分量场")
    return grid, point


def iso_values(values, limits):
    if values is None or values == "":
        return (limits[0] / 2 + limits[1] / 2,)
    if isinstance(values, str):
        values = values.replace("，", ",").split(",")
    elif not isinstance(values, (tuple, list)):
        values = (values,)
    if not 1 <= len(values) <= 32:
        raise FilterInputError("请输入 1–32 个等值")
    return tuple(dict.fromkeys(finite_number(value, "等值") for value in values))


def contour(vtk, grid, name, values, *, volume, radius, association="auto"):
    grid, array = point_field(vtk, grid, name, (1,), association=association)
    limits = array.GetRange()
    levels = iso_values(values, limits)
    result = vtk.vtkContourFilter()
    if volume:
        cell_types = vtk.vtkCellTypes()
        distinct = getattr(grid, "GetDistinctCellTypes", grid.GetCellTypes)
        distinct(cell_types)
        types = [cell_types.GetCellType(index) for index in range(cell_types.GetNumberOfTypes())]
        utilities = getattr(vtk, "vtkCellTypeUtilities", vtk.vtkCellTypes)
        volume_types = [value for value in types if utilities.GetDimension(value) == 3]
        if not volume_types:
            raise FilterInputError("体等值面需要三维体单元，可使用表面等值线查看二维数据")
        chain = []
        if len(types) != len(volume_types):
            extract = vtk.vtkExtractCellsByType()
            extract.SetInputData(grid)
            for value in volume_types:
                extract.AddCellType(value)
            result.SetInputConnection(extract.GetOutputPort())
            chain.append(extract)
        else:
            result.SetInputData(grid)
    else:
        geometry = vtk.vtkGeometryFilter()
        geometry.SetInputData(grid)
        result.SetInputConnection(geometry.GetOutputPort())
        chain = [geometry]
    result.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, name)
    for index, value in enumerate(levels):
        result.SetValue(index, value)
    chain.append(result)
    if volume:
        return chain, limits
    tube = vtk.vtkTubeFilter()
    tube.SetInputConnection(result.GetOutputPort())
    tube.SetRadius(radius)
    tube.SetNumberOfSides(6)
    return [*chain, tube], limits


def displacement(vtk, grid, name, scale, *, association="auto"):
    from numpy import isfinite
    from vtk.util.numpy_support import vtk_to_numpy

    grid, _ = point_field(vtk, grid, name, (3,), association=association)
    explicit = vtk.vtkPointSet.SafeDownCast(grid)
    if explicit is None:
        if grid.IsA("vtkImageData"):
            converter = vtk.vtkImageDataToPointSet()
        elif grid.IsA("vtkRectilinearGrid"):
            converter = vtk.vtkRectilinearGridToPointSet()
        else:
            raise FilterInputError("该网格类型无法直接变形")
        converter.SetInputData(grid)
        converter.Update()
        explicit = converter.GetOutput()
    warp = vtk.vtkWarpVector()
    warp.SetInputData(explicit)
    warp.SetInputArrayToProcess(0, 0, 0, vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, name)
    warp.SetScaleFactor(finite_number(scale, "位移倍率"))
    warp.SetOutputPointsPrecision(vtk.vtkAlgorithm.DOUBLE_PRECISION)
    warp.Update()
    points = warp.GetOutput().GetPoints()
    if points is None or not isfinite(vtk_to_numpy(points.GetData())).all():
        raise FilterInputError("变形坐标超出有效数值范围")
    surface = vtk.vtkGeometryFilter()
    surface.SetInputConnection(warp.GetOutputPort())
    return [warp, surface]


def mode_shape(vtk, grid, name, scale, phase, *, imaginary=None, association="auto"):
    import numpy as np
    from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

    grid, real = point_field(vtk, grid, name, (1, 3), association=association)
    angle = math.radians(finite_number(phase, "相位") % 360)
    with np.errstate(over="ignore", invalid="ignore"):
        data = vtk_to_numpy(real).astype("float64") * math.cos(angle)
        if imaginary:
            grid, imag = point_field(vtk, grid, imaginary, (real.GetNumberOfComponents(),), association=association)
            data -= vtk_to_numpy(imag) * math.sin(angle)
        data *= finite_number(scale, "模态倍率")
    if not np.isfinite(data).all():
        raise FilterInputError("模态幅值超出有效数值范围")
    shape = grid.NewInstance()
    shape.ShallowCopy(grid)
    array = numpy_to_vtk(data, deep=True)
    output_name = "VFModeShape"
    while shape.GetPointData().HasArray(output_name):
        output_name += "_"
    array.SetName(output_name)
    shape.GetPointData().AddArray(array)
    if real.GetNumberOfComponents() == 3:
        return displacement(vtk, shape, output_name, 1.0), output_name, array.GetRange(-1)
    surface = vtk.vtkGeometryFilter()
    surface.SetInputData(shape)
    return [surface], output_name, array.GetRange()
