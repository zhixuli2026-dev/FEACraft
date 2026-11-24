"""Create small engineering-result fixtures with native VTK and standard Exodus data."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import vtk
from scipy.io import netcdf_file


STEPS = (0.25, 1.75)
XYZ = ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1))


def tetra(offset=0, field_offset=0):
    points = vtk.vtkPoints()
    for x, y, z in XYZ:
        points.InsertNextPoint(x + offset, y, z)
    grid = vtk.vtkUnstructuredGrid()
    grid.SetPoints(points)
    cell = vtk.vtkTetra()
    for index in range(4):
        cell.GetPointIds().SetId(index, index)
    grid.InsertNextCell(cell.GetCellType(), cell.GetPointIds())
    for attributes, name, values in (
        (grid.GetPointData(), 'Pressure', (field_offset + i for i in range(1, 5))),
        (grid.GetCellData(), 'CellTemperature', [20 + field_offset]),
    ):
        array = vtk.vtkDoubleArray()
        array.SetName(name)
        for value in values:
            array.InsertNextValue(value)
        attributes.SetScalars(array)
    return grid


def write_vtk(writer, path, data):
    writer.SetFileName(str(path))
    writer.SetInputData(data)
    assert writer.Write() == 1, path.name


def exodus(path):
    # The VTK Exodus writer crashes for the known minimal topology on Windows.
    # Emit a NetCDF classic Exodus database and verify it with actual readers.
    def character_rows(db, variable, dimensions, names):
        output = db.createVariable(variable, 'c', dimensions)
        values = np.zeros((len(names), 33), dtype='S1')
        for row, name in enumerate(names):
            data = name.encode('ascii')
            values[row, :len(data)] = np.frombuffer(data, dtype='S1')
        output[:] = values

    with netcdf_file(path, 'w', version=2) as db:
        for name, size in (
            ('len_string', 33), ('num_dim', 3), ('num_nodes', 4), ('num_elem', 1), ('num_el_blk', 1),
            ('num_el_in_blk1', 1), ('num_nod_per_el1', 4), ('time_step', 2), ('num_nod_var', 1), ('num_elem_var', 1),
        ):
            db.createDimension(name, size)
        db.title = 'Two step tetrahedral reference'
        db.api_version = np.float32(5.0)
        db.version = np.float32(5.0)
        db.floating_point_word_size = 8
        db.file_size = 1
        db.int64_status = 0
        db.max_name_length = 32
        for axis, coordinates in zip('xyz', zip(*XYZ)):
            db.createVariable('coord' + axis, 'd', ('num_nodes',))[:] = coordinates
        ids = db.createVariable('eb_prop1', 'i', ('num_el_blk',))
        ids.name = 'ID'
        ids[:] = [1]
        db.createVariable('eb_status', 'i', ('num_el_blk',))[:] = [1]
        connect = db.createVariable('connect1', 'i', ('num_el_in_blk1','num_nod_per_el1'))
        connect.elem_type = 'TETRA'
        connect[:] = [[1,2,3,4]]
        db.createVariable('time_whole', 'd', ('time_step',))[:] = STEPS
        db.createVariable('vals_nod_var1', 'd', ('time_step','num_nodes'))[:] = [[1,2,3,4],[11,12,13,14]]
        db.createVariable('vals_elem_var1eb1', 'd', ('time_step','num_el_in_blk1'))[:] = [[20],[30]]
        db.createVariable('elem_var_tab', 'i', ('num_el_blk','num_elem_var'))[:] = [[1]]
        db.createVariable('node_num_map','i',('num_nodes',))[:] = [1,2,3,4]
        db.createVariable('elem_num_map','i',('num_elem',))[:] = [1]
        character_rows(db, 'name_nod_var', ('num_nod_var','len_string'), ['Pressure'])
        character_rows(db, 'name_elem_var', ('num_elem_var','len_string'), ['CellTemperature'])
        character_rows(db, 'eb_names', ('num_el_blk','len_string'), ['Core'])


def xdmf():
    def frame(time, offset):
        fields = ' '.join(str(offset + i) for i in range(1, 5))
        return f'''<Grid Name="frame-{time}" GridType="Uniform">
  <Time Value="{time}"/>
  <Topology TopologyType="Tetrahedron" NumberOfElements="1">
    <DataItem Dimensions="1 4" NumberType="Int" Format="XML">0 1 2 3</DataItem>
  </Topology>
  <Geometry GeometryType="XYZ">
    <DataItem Dimensions="4 3" NumberType="Float" Precision="8" Format="XML">0 0 0 1 0 0 0 1 0 0 0 1</DataItem>
  </Geometry>
  <Attribute Name="Pressure" AttributeType="Scalar" Center="Node">
    <DataItem Dimensions="4" NumberType="Float" Precision="8" Format="XML">{fields}</DataItem>
  </Attribute>
  <Attribute Name="CellTemperature" AttributeType="Scalar" Center="Cell">
    <DataItem Dimensions="1" NumberType="Float" Precision="8" Format="XML">{20 + offset}</DataItem>
  </Attribute>
</Grid>'''
    return ('<?xml version="1.0"?>\n<Xdmf Version="2.0"><Domain><Grid Name="reference" '
            'GridType="Collection" CollectionType="Temporal">\n' + frame(STEPS[0],0) + '\n' +
            frame(STEPS[1],10) + '\n</Grid></Domain></Xdmf>\n')


def fixtures(root):
    root.mkdir(parents=True, exist_ok=True)
    original = tetra()
    newer = tetra(field_offset=10)
    legacy = vtk.vtkDataSetWriter()
    legacy.SetFileTypeToBinary()
    write_vtk(legacy, root / 'reference.vtk', original)
    write_vtk(vtk.vtkXMLUnstructuredGridWriter(), root / 'step-start.vtu', original)
    write_vtk(vtk.vtkXMLUnstructuredGridWriter(), root / 'step-end.vtu', newer)
    surface = vtk.vtkGeometryFilter()
    surface.SetInputData(original)
    surface.Update()
    write_vtk(vtk.vtkXMLPolyDataWriter(), root / 'surface.vtp', surface.GetOutput())
    stl = vtk.vtkSTLWriter()
    stl.SetFileTypeToBinary()
    write_vtk(stl, root / 'surface.stl', surface.GetOutput())
    group = vtk.vtkMultiBlockDataSet()
    group.SetBlock(0, original)
    group.GetMetaData(0).Set(vtk.vtkCompositeDataSet.NAME(), 'Core')
    group.SetBlock(1, tetra(offset=3, field_offset=100))
    group.GetMetaData(1).Set(vtk.vtkCompositeDataSet.NAME(), 'Bracket')
    top = vtk.vtkMultiBlockDataSet()
    top.SetBlock(0, group)
    top.GetMetaData(0).Set(vtk.vtkCompositeDataSet.NAME(), 'Assembly')
    top.SetBlock(1, tetra(offset=8, field_offset=200))
    top.GetMetaData(1).Set(vtk.vtkCompositeDataSet.NAME(), 'Mount')
    write_vtk(vtk.vtkXMLMultiBlockDataWriter(), root / 'assembly.vtm', top)
    (root / 'reference.series').write_text(json.dumps({'file-series-version':'1.0', 'files':[
        {'name':'step-start.vtu','time':STEPS[0]}, {'name':'step-end.vtu','time':STEPS[1]},
    ]})+'\n', encoding='utf8')
    (root / 'composite.series').write_text(json.dumps({'file-series-version':'1.0', 'files':[
        {'name':'assembly.vtm','time':STEPS[0]}, {'name':'step-end.vtu','time':STEPS[1]},
    ]})+'\n', encoding='utf8')
    content = xdmf()
    for extension in ('xdmf','xmf'):
        (root / f'temporal.{extension}').write_text(content, encoding='utf8')
    canonical = root / 'temporal.exo'
    exodus(canonical)
    for extension in ('e','exodus'):
        shutil.copyfile(canonical, root / f'temporal.{extension}')
    return [path for path in sorted(root.rglob('*')) if path.is_file()]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    output = parser.parse_args().root.resolve()
    for path in fixtures(output):
        with path.open('rb') as stream:
            print(path.relative_to(output).as_posix(), path.stat().st_size, hashlib.file_digest(stream,'sha256').hexdigest())
