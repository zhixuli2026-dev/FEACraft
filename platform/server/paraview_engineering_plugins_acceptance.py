"""Independently read native engineering outputs after managed job publication."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request


DATAMINE = {
    'datamine_smallpt.dm': '2b6633c466646225d5f78a3d7db7c35abf1d628b5de825f501b090b1872bf13605b6fa48cbb098dc44e3010e530ea42f1ba9320f51f11b2f512f379db404208e',
    'datamine_smalltr.dm': '380ea0f3c6db750edda2284cf1b51d7d8c0e7f85c0143376fa2b668747e406d7e7994623161cafe1b0a5218fd6ccc9a0985056c98e06106c9dc81f8d21305a84',
}


def make_inputs(root):
    from vtkmodules.vtkCommonCore import vtkDoubleArray, vtkIntArray, vtkPoints
    from vtkmodules.vtkCommonDataModel import VTK_HEXAHEDRON, VTK_WEDGE, vtkTable, vtkUnstructuredGrid
    from vtkmodules.vtkIOXML import vtkXMLTableWriter, vtkXMLUnstructuredGridWriter

    def save(name, points, cell_type, arrays):
        coordinates = vtkPoints()
        coordinates.SetDataTypeToDouble()
        for point in points:
            coordinates.InsertNextPoint(*point)
        mesh = vtkUnstructuredGrid()
        mesh.SetPoints(coordinates)
        mesh.InsertNextCell(cell_type,len(points),list(range(len(points))))
        for array_name, array_type, values in arrays:
            array = array_type()
            array.SetName(array_name)
            array.SetNumberOfComponents(len(values))
            array.InsertNextTuple(values)
            mesh.GetCellData().AddArray(array)
        exporter = vtkXMLUnstructuredGridWriter()
        exporter.SetInputData(mesh)
        exporter.SetFileName(str(root/name))
        assert exporter.Write()==1

    square = ((0,0),(1,0),(1,1),(0,1))
    block = [(x,y,z) for z in (0,1) for x,y in square]
    save('xfem.vtu',block,VTK_HEXAHEDRON,[('xfem_cut_origin_',vtkDoubleArray,(.5,0,0)),
                                           ('xfem_cut_normal_',vtkDoubleArray,(1,0,0))])
    save('missing-arrays.vtu',block,VTK_HEXAHEDRON,[])
    triangle = ((0,0),(0,1),(1,0))
    save('layer.vtu',[(x,y,z) for z in (0,.1) for x,y in triangle],VTK_WEDGE,
         [('Thickness',vtkDoubleArray,(.25,)),('Layer',vtkIntArray,(0,))])
    table = vtkTable()
    for index in range(12):
        column = vtkDoubleArray()
        column.SetName(f'sample{index}')
        for step in range(24):
            column.InsertNextValue(.5*index+step*(index%4+1)/25+((index*index+step*step+4*index*step)%17)/30)
        table.AddColumn(column)
    writer = vtkXMLTableWriter()
    writer.SetInputData(table)
    writer.SetFileName(str(root/'observations.vtt'))
    assert writer.Write()==1


def make_surface(path):
    from paraview import simple

    sphere = simple.Sphere(ThetaResolution=64,PhiResolution=48)
    flow = simple.Calculator(Input=sphere)
    flow.ResultArrayName = 'Velocity'
    flow.Function = '-coordsY*iHat+coordsX*jHat'
    displacement = simple.Calculator(Input=flow)
    displacement.ResultArrayName = 'Height'
    displacement.Function = 'coordsZ+0.15*sin(18*coordsY)'
    simple.SaveData(str(path),proxy=displacement)
    assert path.is_file()


def make_panorama(path):
    from paraview import simple

    centers = [(0,0,-3),(0,0,3),(3,0,0),(-3,0,0),(0,3,0),(0,-3,0)]
    objects = [simple.Sphere(Center=center,Radius=.85,ThetaResolution=48,PhiResolution=28) for center in centers]
    scene = simple.AppendDatasets(Input=objects)
    mesh = simple.ExtractSurface(Input=scene)
    simple.SaveData(str(path),proxy=mesh)
    assert path.is_file()


def make_lanl(path):
    """ParaView 5.11.2 fixed-width LANL X3D blocks for a unit tetrahedron."""
    header = {'process':1,'numdim':3,'materials':1,'nodes':4,'faces':4,'elements':1,
              'ghost_nodes':0,'slaved_nodes':0,'nodes_per_slave':0,'nodes_per_face':3,
              'faces_per_cell':4,'node_data_fields':1,'cell_data_fields':3}
    lines = ['x3dtoflag ascii','header',*(f'   {key:<23}{value:10d}' for key,value in header.items()),'end_header']
    for block, name in (('matnames','one'),('mateos','none'),('matopc','none')):
        lines.extend((block,f'   {1:10d}   {name}','end_'+block))
    coordinates = ((0,0,0),(1,0,0),(0,1,0),(0,0,1))
    lines.append('nodes')
    for index, point in enumerate(coordinates,1):
        lines.append(f'{index:10d}'+''.join(f' {value:22.14E}' for value in point))
    lines.extend(('end_nodes','faces'))
    for index, face in enumerate(((1,3,2),(1,2,4),(2,3,4),(3,1,4)),1):
        lines.append(''.join(f'{value:10d}' for value in (index,3,*face,1,0,0,1,1,1,1,1)))
    lines.extend(('end_faces','cells',''.join(f'{v:10d}' for v in (1,4,1,2,3,4)),'end_cells'))
    for block in ('slaved_nodes','ghost_nodes'):
        lines.extend((f'{block:<12}{0:10d}','end_'+block))
    lines.extend(('cell_data','matid',f'{1:10d}','end_matid','partelm',f'{1:10d}','end_partelm',
                  'temperature',f'{293.15:20.12E}','end_temperature','end_cell_data','node_data','velocity'))
    lines.extend(''.join(f'{value:20.12E}' for value in point) for point in coordinates)
    lines.extend(('end_velocity','end_node_data','end_dump'))
    path.write_text('\n'.join(lines)+'\n',encoding='ascii')


def inspect_native(file, validation):
    from vtkmodules.vtkIOXML import vtkXMLMultiBlockDataReader, vtkXMLPolyDataReader, vtkXMLUnstructuredGridReader
    from vtkmodules.vtkFiltersVerdict import vtkCellSizeFilter

    reader = {'.vtm':vtkXMLMultiBlockDataReader,'.vtp':vtkXMLPolyDataReader,'.vtu':vtkXMLUnstructuredGridReader}[file.suffix]()
    reader.SetFileName(str(file))
    reader.Update()
    mesh = reader.GetOutputDataObject(0)
    if validation == 'lanl':
        leaves = []
        iterator = mesh.NewIterator()
        iterator.InitTraversal()
        while not iterator.IsDoneWithTraversal():
            data = iterator.GetCurrentDataObject()
            if data.IsA('vtkUnstructuredGrid'):
                leaves.append(data)
            iterator.GoToNextItem()
        assert len(leaves) == 1
        volume_mesh = leaves[0]
        assert (volume_mesh.GetNumberOfPoints(),volume_mesh.GetNumberOfCells()) == (4,1)
        assert volume_mesh.GetBounds() == (0,1,0,1,0,1)
        cells = volume_mesh.GetCellData()
        assert cells.GetArray('matid').GetValue(0) == 1
        temperature = cells.GetArray('temperature').GetTuple1(0)
        assert abs(temperature-293.15) < 1e-5
        velocity = volume_mesh.GetPointData().GetArray('velocity')
        assert [velocity.GetTuple3(index) for index in range(4)] == [volume_mesh.GetPoint(index) for index in range(4)]
        sizes = vtkCellSizeFilter()
        sizes.SetInputData(volume_mesh)
        sizes.Update()
        volume = sizes.GetOutput().GetCellData().GetArray('Volume').GetTuple1(0)
        assert abs(volume-1/6) < 1e-9
        return {'points':4,'cells':1,'volume':volume,'temperature':temperature,'velocity_tuples':4}
    if validation == 'datamine':
        points,triangles = mesh.GetNumberOfPoints(),mesh.GetNumberOfPolys()
        assert (points,triangles) == (60942,155378)
        attributes = mesh.GetCellData()
        fields = [attributes.GetArrayName(index) for index in range(attributes.GetNumberOfArrays())]
        assert set(fields) == {'TRIANGLE','PID1','PID2','PID3','COLOUR','START'}
        assert all(attributes.GetAbstractArray(name).GetNumberOfTuples()==triangles for name in fields)
        return {'points':points,'triangles':triangles,'cell_fields':fields,'bounds':list(mesh.GetBounds())}
    if validation == 'bag-plot':
        from math import isfinite

        assert mesh.GetNumberOfBlocks() == 4
        functions, projection, grid, thresholds = (mesh.GetBlock(index) for index in range(4))
        assert (functions.GetNumberOfRows(), projection.GetNumberOfRows(), thresholds.GetNumberOfRows()) == (24,12,6)
        assert grid.GetDimensions() == (24,24,1)
        density = grid.GetPointData().GetScalars()
        values = [density.GetTuple1(index) for index in range(density.GetNumberOfTuples())]
        assert len(values) == 576 and all(isfinite(value) and value >= 0 for value in values)
        assert 0 <= min(values) < max(values)
        return {'function_rows':24,'projected_observations':12,'threshold_rows':6,
                'density_dimensions':list(grid.GetDimensions()),'density_range':[min(values),max(values)]}
    if validation == 'particle-source':
        totals = []
        for depth in range(mesh.GetNumberOfBlocks()):
            level = mesh.GetBlock(depth)
            totals.append([level.GetBlock(index).GetNumberOfPoints() for index in range(level.GetNumberOfBlocks())])
        assert totals == [[16],[16]*8],totals
        return {'points_per_block':totals, 'total_points':sum(sum(row) for row in totals)}
    file_bounds = list(mesh.GetBounds())
    cells = mesh.GetNumberOfCells()
    assert cells > 0
    cell_bounds = []
    for index in range(cells):
        region = [0.0]*6
        mesh.GetCellBounds(index,region)
        cell_bounds.append(region)
    bounds = [(min if side % 2 == 0 else max)(region[side] for region in cell_bounds) for side in range(6)]
    if validation == 'layer-thickness':
        assert mesh.GetNumberOfCells()==1 and abs(bounds[5]-bounds[4]-.25)<1e-8,bounds
    else:
        assert abs(bounds[0])<1e-8 and abs(bounds[1]-.5)<1e-8,bounds
    volumes = vtkCellSizeFilter()
    volumes.SetInputData(mesh)
    volumes.Update()
    column = volumes.GetOutputDataObject(0).GetCellData().GetArray('Volume')
    total = sum(column.GetTuple1(index) for index in range(mesh.GetNumberOfCells()))
    assert abs(total-(.125 if validation=='layer-thickness' else .5)) < 1e-8,total
    return {'cell_bounds':bounds,'file_bounds':file_bounds,'cells':cells,'volume':total}


def acceptance(native, *, bag_only=False, datamine=None, representations=False, panorama=False, lanl=False,
               streaming=False, keyframes=False):
    from testsupport import IsolatedServer

    api = IsolatedServer('ff-d20-engineering-plugins')
    print('D20_EVIDENCE '+str(api.root),flush=True)
    inputs = api.root/'inputs'
    inputs.mkdir()
    worker = str(Path(__file__).resolve())
    options = [str(native),'--no-mpi'] if sys.platform!='win32' else [str(native)]
    if lanl:
        make_lanl(inputs/'tetrahedron.x3d')
    elif datamine is None and not (streaming or keyframes):
        if panorama:
            generate = ['--make-panorama',str(inputs/'panorama.vtp')]
        elif representations:
            generate = ['--make-surface',str(inputs/'surface.vtp')]
        else:
            generate = ['--make-inputs',str(inputs)]
        subprocess.run([*options,worker,*generate],check=True,timeout=45)
    evidence = {'passed':False,'actual_model_calls':0,'mock_llm_calls':0,'plugins':{}}

    def save():
        (api.root/'engineering-plugins.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')

    def upload(filename, source=inputs):
        data = (source/filename).read_bytes()
        if filename in DATAMINE:
            assert hashlib.sha512(data).hexdigest() == DATAMINE[filename],filename
        path = 'D-post/'+filename
        boundary = 'vf-native-plugin-input'
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                   'Content-Type: application/octet-stream\r\n\r\n').encode()+data+f'\r\n--{boundary}--\r\n'.encode()
        request = Request(api.base+'/api/v1'+api.prefix+'/upload?'+urlencode({'path':path}),data=payload,
                          headers={'Authorization':'Bearer '+api.token,
                                   'Content-Type':f'multipart/form-data; boundary={boundary}'})
        with api.opener.open(request,timeout=30) as response:
            assert json.load(response)['content_sha256']==hashlib.sha256(data).hexdigest()
        return {'id':'input','group':'file','file':path}

    def filter_node(name,settings=None):
        return {'id':'result','group':'filters','name':name,'inputs':{'Input':['input']},'properties':settings or {}}

    def settle(identifier, expected='succeeded'):
        deadline = time.monotonic()+100
        while time.monotonic()<deadline:
            value = api.call('GET',f'/jobs/{identifier}')
            if value['ended_at'] and not value['completion_pending']:
                assert value['status']==expected,value['log_backlog'][-1600:]
                return value
            time.sleep(.12)
        raise TimeoutError('Native ParaView did not settle')

    try:
        with api:
            api.create_case('Native long-tail engineering plugins')
            router=api.prefix+'/paraview'

            if keyframes:
                node = {'id':'sphere','group':'sources','name':'SphereSource',
                        'properties':{'ThetaResolution':24,'PhiResolution':20}}
                animation = {'fps':2,'tracks':[{'node':'sphere','property':'Radius',
                                               'frames':[{'t':0,'value':.4},{'t':1,'value':.8}]}]}
                opened = api.call('POST',router+'/pipeline',{'nodes':[node],'animation':animation})
                done = settle(opened['job_id'])
                report = api.call('GET',api.prefix+'/raw?'+urlencode({'path':opened['report_path']}))
                measured = report['animation']['frames']
                assert [item['t'] for item in measured] == [0,.5,1]
                radii = [item['bounds'][-1] for item in measured]
                assert all(abs(actual-expected)<1e-6 for actual,expected in zip(radii,(.4,.6,.8)))
                hashes = [done['attempt']['outputs']['files'][frame['path']]['sha256'] for frame in measured]
                assert len(set(hashes)) == 3
                evidence.update(passed=True,keyframes=measured,frame_sha256=hashes)
                return

            if panorama:
                from io import BytesIO
                from PIL import Image, ImageChops

                scene = upload('panorama.vtp')
                plugin = 'PanoramicProjectionView'
                phases = [('Equirectangular',360,(1024,512)),('Equirectangular',180,(1024,512)),
                          ('Azimuthal (fish eye)',360,(800,800))]
                for index,(mode,angle,size) in enumerate(phases):
                    projection = {'mode':mode,'angle':angle}
                    reply = api.call('POST',router+'/pipeline',{'nodes':[scene],'plugins':[plugin],
                        'backend':'pvpython','inspection':{'view':'front','projection':projection}})
                    done = settle(reply['job_id'])
                    raw = lambda path: api.prefix+'/raw?'+urlencode({'path':path})
                    report = api.call('GET',raw(reply['report_path']))
                    assert report['inspection']['projection'] == {**projection,'size':list(size)}
                    path = report['inspection']['image']
                    image = api.call('GET',raw(path),raw=True)
                    digest = hashlib.sha256(image).hexdigest()
                    assert digest == done['attempt']['outputs']['files'][path]['sha256']
                    with Image.open(BytesIO(image)) as native_image:
                        assert native_image.format == 'PNG' and native_image.size == size
                        picture = native_image.convert('RGB')
                    assert picture.getextrema()[0][1] > 60
                    name = f'{mode.split()[0]}-{angle}'
                    if index == 0:
                        reference = picture
                    elif index == 1:
                        diff = ImageChops.difference(reference,picture).convert('L').histogram()
                        assert sum(diff[16:]) > 3000
                    else:
                        assert picture.getpixel((0,0)) == (0,0,0) and picture.getpixel((400,400)) != (0,0,0)
                    (api.root/(name+'.png')).write_bytes(image)
                    evidence['plugins'][name] = {'report':report,'sha256':digest,'resolution':list(size)}
                    save()
                    print('PANORAMA_PASS '+name,flush=True)
                evidence['passed'] = True
                return

            if representations:
                from io import BytesIO
                from PIL import Image, ImageChops

                surface = upload('surface.vtp')
                choices = [('Surface',None,None),('Surface LIC','SurfaceLIC','Velocity'),
                           ('Stream Lines','StreamLinesRepresentation','Velocity'),
                           ('Bump Mapped Surface','EmbossingRepresentations','Height'),
                           ('Extrusion Surface','EmbossingRepresentations','Height')]
                for mode, plugin, field in choices:
                    settings = {'view':'front'}
                    if plugin:
                        settings['representation'] = {'mode':mode,'array':['POINTS',field]}
                    reply = api.call('POST',router+'/pipeline',{'nodes':[surface],
                        'plugins':[plugin] if plugin else [],'backend':'pvpython','inspection':settings})
                    done = settle(reply['job_id'])
                    raw = lambda path: api.prefix+'/raw?'+urlencode({'path':path})
                    report = api.call('GET',raw(reply['report_path']))
                    image_path = report['inspection']['image']
                    image = api.call('GET',raw(image_path),raw=True)
                    digest = hashlib.sha256(image).hexdigest()
                    assert digest == done['attempt']['outputs']['files'][image_path]['sha256']
                    picture = Image.open(BytesIO(image)).convert('RGB')
                    assert picture.size == (800,600)
                    if plugin:
                        assert report['inspection']['representation'] == settings['representation']
                        difference = ImageChops.difference(reference,picture).convert('L').histogram()
                        changed = sum(difference[12:])
                        assert changed > 250,(mode,changed)
                        evidence['plugins'][mode] = {'report':report,'sha256':digest,'changed_pixels':changed}
                    else:
                        reference = picture
                    (api.root/(mode.replace(' ','-')+'.png')).write_bytes(image)
                    save()
                    print('REPRESENTATION_PASS '+mode,flush=True)
                evidence['passed'] = True
                return

            def pipeline(plugin,nodes,validation,animation=None):
                opened = api.call('POST',router+'/pipeline',{'nodes':nodes,'plugins':[plugin],'backend':'pvpython',
                    'animation':animation})
                done = settle(opened['job_id'])
                args = lambda path: api.prefix+'/raw?'+urlencode({'path':path})
                report = api.call('GET',args(opened['report_path']))
                assert report['plugins'][0]['loaded'] and report['plugins'][0]['name']==plugin
                folder = PurePosixPath(report['result']).parent
                downloaded = api.root/validation
                downloaded.mkdir()
                for relative in done['attempt']['outputs']['files']:
                    file = PurePosixPath(relative)
                    if not file.is_relative_to(folder) or not file.relative_to(folder).parts[0].startswith('result'):
                        continue
                    target = downloaded/file.relative_to(folder)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(api.call('GET',args(relative),raw=True))
                selected = downloaded/PurePosixPath(report['result']).name
                assert selected.is_file()
                receipt = downloaded/'native-inspection.json'
                subprocess.run([*options,worker,'--inspect',str(selected),validation,str(receipt)],check=True,timeout=45)
                evidence['plugins'][plugin] = {'report':report,'native':json.loads(receipt.read_bytes()),
                    'sha256':hashlib.sha256(selected.read_bytes()).hexdigest()}
                if validation == 'bag-plot':
                    from io import BytesIO
                    from PIL import Image

                    chart = api.call('GET',args(report['chart']),raw=True)
                    expected = done['attempt']['outputs']['files'][report['chart']]['sha256']
                    assert hashlib.sha256(chart).hexdigest() == expected
                    (downloaded/'bag-plot.png').write_bytes(chart)
                    with Image.open(BytesIO(chart)) as picture:
                        assert picture.format == 'PNG' and picture.size == (900,560)
                        colors = picture.convert('RGB').getcolors(picture.width*picture.height)
                    assert sum(count for count,(red,green,blue) in colors if red > max(green,blue)+40) > 5000
                    evidence['plugins'][plugin]['chart'] = {'sha256':expected,'size':[900,560],'colors':len(colors)}
                save()
                print('PLUGIN_PASS '+plugin,flush=True)
                return report

            if streaming:
                from io import BytesIO
                from PIL import Image

                plugin = 'StreamingParticles'
                source = {'id':'points','group':'sources','name':'RandomPoints',
                          'properties':{'NumberOfLevels':2,'PointsPerBlock':16,'Seed':19}}
                report = pipeline(plugin,[source],'particle-source',animation={'streaming':16,'fps':2})
                frames = report['animation']['frames']
                assert [frame['points'] for frame in frames] == [16*index for index in range(1,10)]
                assert {tuple(block) for frame in frames for block in frame['blocks']} == {(0,0),*((1,index) for index in range(8))}
                hashes = []
                for index,frame in enumerate(frames):
                    path = api.prefix+'/raw?'+urlencode({'path':frame['path']})
                    content = api.call('GET',path,raw=True)
                    with Image.open(BytesIO(content)) as image:
                        assert image.size == (640,480) and len(image.getcolors(image.width*image.height)) > 4
                    (api.root/f'particle-{index:02d}.png').write_bytes(content)
                    hashes.append(hashlib.sha256(content).hexdigest())
                assert len(set(hashes)) == 9
                evidence.update(passed=True,frame_sha256=hashes)
                return

            if datamine is not None:
                samples = [upload(name,datamine) for name in DATAMINE]
                source = {**samples[0],'reader':'DataMineWireFrameReader'}
                pipeline('Datamine',[source],'datamine')
                evidence.update(passed=True,fixture_sha512=DATAMINE)
                return
            if lanl:
                reader = 'LANLX3DReader'
                pipeline(reader,[{**upload('tetrahedron.x3d'),'reader':reader}],'lanl')
                evidence['passed'] = True
                return

            table = upload('observations.vtt')
            table_info = api.call('GET',router+'/dataset?'+urlencode({'path':table['file']}))
            assert table_info['dataset'] == 'vtkTable'
            assert {item['name'] for item in table_info['arrays'] if item['tuples'] == 24} == {
                f'sample{index}' for index in range(12)}
            pipeline('BagPlotViewsAndFilters',[table,filter_node('ExtractBagPlots',{
                'SelectArrays':[f'sample{index}' for index in range(12)],'GridSize':24})],'bag-plot')
            if not bag_only:
                xfem, layer, missing = (upload(name) for name in ('xfem.vtu','layer.vtu','missing-arrays.vtu'))
                pipeline('MooseXfemClip',[xfem,filter_node('MooseXfemClip')],'xfem-clip')
                pipeline('ThickenLayeredCells',[layer,filter_node('ThickenCells',{
                    'SelectThickness':['CELLS','Thickness'],'SelectLayer':['CELLS','Layer']})],'layer-thickness')
                pipeline('StreamingParticles',[{'id':'points','group':'sources','name':'RandomPoints',
                    'properties':{'NumberOfLevels':2,'PointsPerBlock':16,'Seed':19}}],'particle-source')
                invalid = api.call('POST',router+'/pipeline',{'nodes':[missing,filter_node('MooseXfemClip')],
                    'plugins':['MooseXfemClip'],'backend':'pvpython'})
                failed = settle(invalid['job_id'],expected='failed')
                assert 'xfem_cut_origin_' in failed['log_backlog']
                evidence['missing_cell_arrays_rejected'] = True
            evidence['passed'] = True
    finally:
        save()


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--make-inputs',type=Path)
    parser.add_argument('--make-surface',type=Path)
    parser.add_argument('--make-panorama',type=Path)
    parser.add_argument('--inspect',nargs=3)
    parser.add_argument('--native',type=Path)
    selected = parser.add_mutually_exclusive_group()
    selected.add_argument('--bag-only',action='store_true')
    selected.add_argument('--datamine-data',type=Path)
    selected.add_argument('--representations',action='store_true')
    selected.add_argument('--panorama',action='store_true')
    selected.add_argument('--lanl',action='store_true')
    selected.add_argument('--streaming',action='store_true')
    selected.add_argument('--keyframes',action='store_true')
    args = parser.parse_args()
    if args.make_inputs:
        make_inputs(args.make_inputs)
    elif args.make_surface:
        make_surface(args.make_surface)
    elif args.make_panorama:
        make_panorama(args.make_panorama)
    elif args.inspect:
        artifact,validation,output = args.inspect
        Path(output).write_text(json.dumps(inspect_native(Path(artifact),validation)),encoding='utf8')
    elif args.native:
        acceptance(args.native.resolve(strict=True),bag_only=args.bag_only,datamine=args.datamine_data,
                   representations=args.representations,panorama=args.panorama,lanl=args.lanl,streaming=args.streaming,
                   keyframes=args.keyframes)
    else:
        parser.error('Select the actual local ParaView executable with --native')
