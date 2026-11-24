"""Evaluate typed properties and connections inside the owned ParaView process."""

import json
import math
from bisect import bisect_left
from pathlib import Path

from paraview import servermanager as sm, simple


EXTENSIONS = {
    'vtkPolyData': '.vtp', 'vtkUnstructuredGrid': '.vtu', 'vtkImageData': '.vti',
    'vtkRectilinearGrid': '.vtr', 'vtkStructuredGrid': '.vts', 'vtkMultiBlockDataSet': '.vtm',
    'vtkTable': '.vtt',
}
PARALLEL_WRITERS = {'VTP', 'VTU', 'VTI', 'VTR', 'VTS'}


def domains(prop):
    iterator = prop.NewDomainIterator()
    iterator.Begin()
    while not iterator.IsAtEnd():
        yield iterator.GetDomain()
        iterator.Next()


def array_selection(proxy, name, value, prop):
    if (not isinstance(value, list) or len(value) != 2 or value[0] not in ('POINTS', 'CELLS', 'FIELD', 'ROWS')
            or not isinstance(value[1], str)):
        raise ValueError(f'{name} requires [association, array name]')
    choices = set()
    for domain in domains(prop):
        if domain.IsA('vtkSMArrayListDomain'):
            choices.update((sm.GetAssociationAsString(domain.GetFieldAssociation(index)), domain.GetString(index))
                           for index in range(domain.GetNumberOfStrings()))
    if tuple(value) not in choices:
        raise ValueError(f'{name}: array {value[1]} is unavailable for {value[0]}')
    proxy.SetPropertyWithName(name, value)


def properties(proxy, settings):
    for name, value in settings.items():
        native = proxy.SMProxy.GetProperty(name)
        wrapped = proxy.GetProperty(name)
        if (native is None or wrapped is None or native.GetInformationOnly()
                or native.IsA('vtkSMInputProperty') or native.GetIsInternal()):
            raise ValueError(f'Property is not editable: {name}')
        if isinstance(wrapped, sm.ArraySelectionProperty):
            array_selection(proxy, name, value, native)
        elif isinstance(wrapped, sm.ProxyProperty):
            if not isinstance(value, dict) or set(value) != {'type', 'properties'} or value['type'] not in wrapped.Available:
                raise ValueError(f'{name} requires a declared proxy type and its properties')
            proxy.SetPropertyWithName(name, value['type'])
            properties(wrapped.GetData(), value['properties'])
        else:
            if any(domain.IsA('vtkSMFileListDomain') for domain in domains(native)):
                raise ValueError('Use a case file node for dataset inputs')
            values = value if isinstance(value, list) else [value]
            if any(not isinstance(item, (bool, int, float, str)) or
                   isinstance(item, (int, float)) and not math.isfinite(item) for item in values):
                raise ValueError(f'{name} requires finite scalar or vector values')
            proxy.SetPropertyWithName(name, value)
            if not native.IsInDomains():
                raise ValueError(f'{name} is outside its native ParaView domain')


def operator(row, previous, controller):
    native = sm.ProxyManager().NewProxy(row['group'], row['name'])
    if native is None:
        raise ValueError(f'Installed ParaView has no operator {row["name"]}')
    proxy = sm._getPyProxy(native)
    controller.PreInitializeProxy(proxy)
    if row['group']=='filters':
        definitions = sm.ActiveConnection.Session.GetProxyDefinitionManager()
        definition = definitions.GetCollapsedProxyDefinition(row['group'],row['name'],None)
        for index in range(definition.GetNumberOfNestedElements()):
            port = definition.GetNestedElement(index)
            if (port.GetName()=='InputProperty' and port.GetAttribute('optional')!='1'
                    and port.GetAttribute('is_internal')!='1' and port.GetAttribute('name') not in row['inputs']):
                raise ValueError(f'Missing connection for {row["name"]}.{port.GetAttribute("name")}')
    for name, references in row['inputs'].items():
        prop = native.GetProperty(name)
        if prop is None or not prop.IsA('vtkSMInputProperty') or (len(references) > 1 and not prop.GetRepeatable()):
            raise ValueError(f'Invalid connection for {row["name"]}.{name}')
        proxy.SetPropertyWithName(name, [previous[ref] for ref in references])
    controller.PostInitializeProxy(proxy)
    for name in row['inputs']:
        if not native.GetProperty(name).IsInDomains():
            raise ValueError(f'{row["name"]}.{name} rejects the connected data')
    if row['name'] == 'ExtractBagPlots':
        from vtkmodules.vtkCommonCore import vtkDataArray

        source = previous[row['inputs']['Input'][0]]
        columns = row['properties'].get('SelectArrays')
        if not isinstance(columns, list) or len(columns) < 2 or not all(isinstance(name, str) for name in columns):
            raise ValueError('Extract Bag Plots requires at least two table columns')
        table = source.GetDataInformation().GetRowDataInformation()
        for name in columns:
            array = table.GetArrayInformation(name)
            if array is None or array.GetNumberOfComponents() != 1 or vtkDataArray.CreateDataArray(array.GetDataType()) is None:
                raise ValueError(f'Extract Bag Plots requires a numeric column: {name}')
    if row['name'] == 'ThickenCells':
        source = previous[row['inputs']['Input'][0]]
        if source.GetDataInformation().GetDataSetTypeAsString() != 'vtkUnstructuredGrid':
            raise ValueError('Thicken Layered Cells requires an unstructured grid')
        settings = row['properties']
        if settings.get('EnableThickening', 1) not in (0, False, [0]):
            for name in ('SelectThickness', 'SelectLayer'):
                selection = settings.get(name)
                if not isinstance(selection, list) or len(selection) != 2 or selection[0] != 'CELLS':
                    raise ValueError(f'Thicken Layered Cells requires a cell array for {name}')
    if row['name'] == 'MooseXfemClip':
        source = previous[row['inputs']['Input'][0]]
        for array_name in ('xfem_cut_origin_', 'xfem_cut_normal_'):
            array = source.CellData.GetArray(array_name)
            if array is None or array.GetNumberOfComponents() != 3:
                raise ValueError(f'MooseXfemClip requires cell array {array_name} with three components')
    properties(proxy, row['properties'])
    controller.RegisterPipelineProxy(proxy, row['id'])
    return proxy


def sample(frames, moment, key, interpolation='linear'):
    right = bisect_left([frame['t'] for frame in frames], moment)
    if right == 0 or right == len(frames) or frames[right]['t'] == moment:
        return frames[min(right,len(frames)-1)][key]
    before, after = frames[right-1], frames[right]
    if interpolation == 'step':
        return before[key]
    mix = (moment-before['t'])/(after['t']-before['t'])
    if interpolation == 'smooth':
        mix = mix*mix*(3-2*mix)
    first, last = before[key], after[key]
    if isinstance(first,list):
        return [(1-mix)*a+mix*b for a,b in zip(first,last)]
    return (1-mix)*first+mix*last


def apply_frame(visual, tracks, targets, moment):
    for track in tracks:
        prop = track['property']
        proxy = targets[track['node']]
        value = sample(track['frames'],moment,'value',track['interpolation'])
        native = proxy.SMProxy.GetProperty(prop)
        if native and native.IsA('vtkSMIntVectorProperty') and track['interpolation']!='step':
            value = [round(item) for item in value] if isinstance(value,list) else round(value)
        properties(proxy, {prop:value})
    visual.UpdatePipeline()
    return visual.GetDataInformation()


def frame_sequence(visual, animation, targets, view):
    bounds = []
    for moment in animation['times']:
        data = apply_frame(visual, animation['tracks'], targets, moment)
        if data.GetNumberOfPoints():
            bounds.append(data.GetBounds())
    if not bounds:
        raise ValueError('The animated pipeline has no renderable geometry')
    fit_animation_camera(view,bounds)


def fit_animation_camera(view, bounds):
    low = [min(box[2*i] for box in bounds) for i in range(3)]
    high = [max(box[2*i+1] for box in bounds) for i in range(3)]
    center = [(a+b)*.5 for a,b in zip(low,high)]
    diagonal = max(math.dist(low,high), 1e-9)
    view.CameraPosition = [value+diagonal for value in center]
    view.CameraFocalPoint = center
    view.CameraViewUp = [0,0,1]
    view.CameraParallelProjection = 1
    view.CameraParallelScale = diagonal*.65


def render_animation(visual, animation, proxies, folder):
    output = folder/'frames'
    output.mkdir()
    simple._DisableFirstRenderCameraReset()
    if animation.get('streaming'):
        return capture_particle_stream(visual,animation,output)
    view = simple.CreateView('RenderView')
    view.UseColorPaletteForBackground = 0
    view.BackgroundColorMode = 'Single Color'
    view.Background = [0.09,0.10,0.12]
    flipbook = animation.get('flipbook')
    fixed_camera = animation['camera']
    if flipbook:
        sequence = [proxies[node] for node in flipbook]
        bounds = [proxy.GetDataInformation() for proxy in sequence]
        if any(not dataset.GetNumberOfPoints() for dataset in bounds):
            raise ValueError('Each Flip Book node requires renderable geometry')
        displays = [simple.Show(proxy,view) for proxy in sequence]
        fit_animation_camera(view,[dataset.GetBounds() for dataset in bounds])
    else:
        display = simple.Show(visual, view)
        targets = {**proxies, '@display':display}
        if fixed_camera is None:
            frame_sequence(visual,animation,targets,view)
    renderer = view.GetClientSideObject().GetRenderer()
    frames = []
    for index, moment in enumerate(animation['times']):
        if flipbook:
            for position, representation in enumerate(displays):
                representation.Visibility = int(position == index)
            data = sequence[index].GetDataInformation()
        else:
            data = apply_frame(visual, animation['tracks'], targets, moment)
        if fixed_camera:
            keyframes = fixed_camera['frames']
            position = sample(keyframes,moment,'position')
            focal = sample(keyframes,moment,'focal')
            up = sample(keyframes,moment,'up')
            if position == focal or not any(up):
                raise ValueError('The animated camera has a degenerate pose')
            view.CameraPosition, view.CameraFocalPoint, view.CameraViewUp = position, focal, up
        view.Update()
        renderer.ResetCameraClippingRange()
        target = output/f'frame-{index:04d}.png'
        simple.SaveScreenshot(str(target), view, ImageResolution=[640,480])
        if not target.is_file():
            raise ValueError('ParaView did not save an animation frame')
        frames.append({'t':moment, 'path':target.as_posix(), 'points':data.GetNumberOfPoints(),
                       'bounds':list(data.GetBounds()) if data.GetNumberOfPoints() else None})
        if flipbook:
            frames[-1]['node'] = flipbook[index]
    result = {'fps':animation['fps'], 'frames':frames, 'tracks':animation['tracks'], 'camera':fixed_camera}
    if flipbook:
        result['flipbook'] = flipbook
    return result


def capture_particle_stream(visual, animation, output):
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersCore import vtkAppendPolyData
    from vtkmodules.vtkFiltersGeneral import vtkVertexGlyphFilter

    bounds = visual.GetDataInformation().GetBounds()
    simple.GetSettingsProxy('GeneralSettings').EnableStreaming = 1
    view = simple.CreateView('RenderView')
    display = simple.Show(visual,view)
    display.Representation = 'Streaming Particles'
    display.StreamingRequestSize = 1
    fit_animation_camera(view,[bounds])
    simple.Render(view)
    active = display.SMProxy.GetClientSideObject().GetActiveRepresentation()
    delivery = view.SMProxy.GetClientSideObject().GetDeliveryManager()
    delivery.SetStreamable(active,True)

    # ParaView 5.11 stages native pieces in a local session even when its delivery
    # proxy reports no client transfer. Render these pieces directly in a local view.
    producer = simple.TrivialProducer()
    preview = simple.CreateView('RenderView')
    preview.OrientationAxesVisibility = 0
    preview.UseColorPaletteForBackground = 0
    preview.BackgroundColorMode = 'Single Color'
    preview.Background = [.09,.10,.12]
    fit_animation_camera(preview,[bounds])
    accumulated = {}
    frames = []
    for index in range(animation['streaming']):
        view.SMProxy.StreamingUpdate(False)
        piece = delivery.GetCurrentStreamedPiece(active)
        if piece is None:
            break
        loaded = []
        for level in range(piece.GetNumberOfBlocks()):
            group = piece.GetBlock(level)
            for block in range(group.GetNumberOfBlocks()):
                data = group.GetBlock(block)
                key = level,block
                if data is not None and data.IsA('vtkPolyData') and data.GetNumberOfPoints() and key not in accumulated:
                    stored = vtkPolyData()
                    stored.DeepCopy(data)
                    accumulated[key] = stored
                    loaded.append([level,block])
        if not loaded:
            break
        combine = vtkAppendPolyData()
        for data in accumulated.values():
            combine.AddInputData(data)
        vertices = vtkVertexGlyphFilter()
        vertices.SetInputConnection(combine.GetOutputPort())
        vertices.Update()
        cloud = vertices.GetOutput()
        producer.GetClientSideObject().SetOutput(cloud)
        producer.SMProxy.MarkModified(producer.SMProxy)
        producer.UpdatePipeline()
        display = simple.Show(producer,preview)
        display.Representation = 'Points'
        display.PointSize = 3
        simple.Render(preview)
        target = output/f'frame-{index:04d}.png'
        if not simple.SaveScreenshot(str(target),preview,ImageResolution=[640,480]) or not target.is_file():
            raise ValueError('ParaView did not render the streamed particle cloud')
        frames.append({'path':target.as_posix(),'t':index/animation['fps'],
                       'points':cloud.GetNumberOfPoints(),'blocks':loaded})
    if not frames:
        raise ValueError('The native particle stream returned no preview blocks')
    return {'fps':animation['fps'],'tracks':[],'camera':None,'frames':frames,'streaming':animation['streaming']}


def run(recipe):
    output = Path(recipe['output'])
    output.mkdir(parents=True, exist_ok=False)
    plugins = [load_plugin(name) for name in recipe.get('plugins') or []]
    controller = sm.ParaViewPipelineController()
    previous, results = {}, []
    for row in recipe['nodes']:
        if row['group'] == 'file':
            file = row.get('files') or row['file']
            if 'reader' in row:
                proxy = read_plugin_file(row,controller)
            else:
                proxy = simple.OpenDataFile(file)
            if proxy is None:
                raise ValueError(f'ParaView cannot open dataset {row["id"]}')
            simple.RenameSource(row['id'], proxy)
        else:
            proxy = operator(row, previous, controller)
        proxy.UpdatePipeline()
        previous[row['id']] = proxy
        data = proxy.GetDataInformation()
        results.append({'id': row['id'], 'points': data.GetNumberOfPoints(), 'cells': data.GetNumberOfCells(),
                        'bounds': list(data.GetBounds()) if data.GetNumberOfPoints() else None,
                        'data_type': data.GetDataSetTypeAsString()})
        if 'reader' in row:
            if not data.GetNumberOfPoints():
                raise ValueError(f'ParaView plugin reader produced no mesh for {row["id"]}')
            results[-1]['reader'] = proxy.SMProxy.GetXMLName()
    ending = results[-1]
    file_type = EXTENSIONS.get(ending['data_type'])
    if file_type is None:
        raise ValueError(f'No native writer for {ending["data_type"]}')
    target = output/('result'+file_type)
    simple.SaveData(str(target), proxy=proxy)
    if not target.is_file():
        raise ValueError('ParaView did not produce a dataset')
    report = {'schema': 'vibeflow.paraview.pipeline.v1', 'nodes': results,
              'result': target.as_posix(), 'state': (output/'pipeline.pvsm').as_posix(), 'recipe': recipe['nodes']}
    if plugins:
        report['plugins'] = plugins
    if proxy.SMProxy.GetXMLName() == 'ExtractBagPlots':
        chart = simple.CreateView('XYFunctionalBagChartView')
        chart.ViewSize = [900, 560]
        simple.Show(proxy, chart)
        image = output/'bag-plot.png'
        simple.SaveScreenshot(str(image), chart, ImageResolution=[900, 560])
        if not image.is_file():
            raise ValueError('ParaView did not render the functional bag plot')
        report['chart'] = image.as_posix()
    if recipe.get('extractors'):
        report['extractors'] = generate_extracts(previous, recipe['extractors'], output/'extracts')
    if recipe.get('inspection'):
        report['inspection'] = inspect_render(proxy, previous, recipe['inspection'], output/'inspection.png')
    simple.SaveState(str(output/'pipeline.pvsm'))
    if recipe.get('animation'):
        report['animation'] = render_animation(proxy, recipe['animation'], previous, output)
    if recipe.get('volume_format'):
        report['volume'] = export_volume(proxy, output, recipe['volume_format'])
    (output/'pipeline.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding='utf-8')


def export_volume(proxy, output, kind):
    if proxy.GetDataInformation().GetDataSetTypeAsString() != 'vtkImageData':
        raise ValueError('Analyze and NIfTI export require vtkImageData')
    data = sm.Fetch(proxy)
    active = data.GetPointData().GetScalars()
    components = active.GetNumberOfComponents() if active is not None else 0
    if components not in ((1,) if kind == 'Analyze' else (1,3,4)):
        raise ValueError(f'{kind} requires compatible active point scalars')
    spacing = data.GetSpacing()
    if kind == 'Analyze' and not all(math.isclose(value,spacing[0],rel_tol=1e-7) for value in spacing[1:]):
        raise ValueError('Use NIfTI for unequal voxel spacing. Analyze in this ParaView release requires isotropic spacing.')
    suffixes = ('.img','.hdr') if kind == 'Analyze' else ('.nii',)
    paths = [output/('volume'+suffix) for suffix in suffixes]
    native = sm.ProxyManager().NewProxy('writers',kind+'Writer')
    if native is None:
        raise ValueError(f'{kind} writer is unavailable')
    writer = sm._getPyProxy(native)
    writer.SetPropertyWithName('Input',proxy)
    writer.SetPropertyWithName('FileName',str(paths[0].resolve()))
    writer.UpdateVTKObjects()
    writer.UpdatePipeline()
    if any(not path.is_file() or path.stat().st_size == 0 for path in paths):
        raise ValueError(f'{kind} native writer did not produce the volume')
    return {'format':kind,'files':[path.as_posix() for path in paths],
            'spacing':list(spacing),'dimensions':list(data.GetDimensions())}


def read_plugin_file(row, controller):
    native = sm.ProxyManager().NewProxy('sources',row['reader'])
    if native is None:
        raise ValueError(f'ParaView plugin reader {row["reader"]} is unavailable')
    proxy = sm._getPyProxy(native)
    controller.PreInitializeProxy(proxy)
    property_name = 'FileNames' if native.GetProperty('FileNames') else 'FileName'
    proxy.SetPropertyWithName(property_name,str(Path(row['file']).resolve()))
    controller.PostInitializeProxy(proxy)
    if row['reader'] == 'DataMineWireFrameReader':
        proxy.UpdatePipelineInformation()
        arrays = proxy.GetProperty('CellArrayStatus')
        proxy.SetPropertyWithName('CellArrayStatus', arrays.Available)
    controller.RegisterPipelineProxy(proxy,row['id'])
    return proxy


def generate_extracts(nodes, rules, output):
    timekeeper = simple.GetTimeKeeper()
    initial_time = timekeeper.Time
    times = list(timekeeper.TimestepValues) or [initial_time]
    controller = sm.vtkSMExtractsController()
    reports = []
    for rule in rules:
        producer = nodes[rule['node']]
        kind = rule['format']
        if kind == 'VTK':
            native_type = producer.GetDataInformation().GetDataSetTypeAsString()
            if native_type not in EXTENSIONS:
                raise ValueError(f'No native VTK extractor for {native_type}')
            kind = EXTENSIONS[native_type][1:].upper()
        if kind == 'PNG':
            view = simple.CreateView('RenderView')
            simple.Show(producer, view)
            view.ResetCamera()
        extractor = simple.CreateExtractor(kind, view if kind == 'PNG' else producer,
                                           registrationName=rule['id'])
        if not extractor:
            raise ValueError(f'ParaView cannot apply extractor {kind} to {rule["node"]}')
        writer = extractor.Writer
        suffix = ('p' if kind in PARALLEL_WRITERS else '')+kind.lower()
        writer.FileName = 'step_{timestep:06d}.'+suffix
        if 'UseSubdirectory' in writer.ListProperties():
            writer.UseSubdirectory = 0
        if kind == 'PNG':
            writer.ImageResolution = [640, 480]
        folder = output/rule['id']
        folder.mkdir(parents=True)
        controller.SetExtractsOutputDirectory(str(folder.resolve()))
        end = len(times)-1 if rule['end'] is None else rule['end']
        if not rule['start'] <= end < min(len(times), 1000):
            raise ValueError(f'Extract range does not match available data times for {rule["id"]}')
        frames = []
        for step in range(rule['start'],end+1,rule['every']):
            moment = times[step]
            producer.UpdatePipeline(moment)
            if kind == 'PNG':
                view.ViewTime = moment
                simple.Render(view)
            controller.SetTime(moment)
            controller.SetTimeStep(step)
            if not controller.Extract(extractor.SMProxy):
                raise ValueError(f'ParaView extraction failed at step {step} for {rule["id"]}')
            target = folder/f'step_{step:06d}.{suffix}'
            if not target.is_file():
                raise ValueError(f'ParaView did not create {target.name}')
            frames.append({'step': step, 'time': moment, 'path': target.as_posix()})
        reports.append({'rule': rule, 'native_format':kind, 'frames': frames,
                        'files':sorted(path.as_posix() for path in folder.rglob('*') if path.is_file())})
    for producer in nodes.values():
        producer.UpdatePipeline(initial_time)
    return reports


MATERIAL_FIELDS = {'color':'DiffuseColor', 'metallic':'Metallic', 'roughness':'Roughness', 'opacity':'Opacity',
                   'coat_strength':'CoatStrength', 'coat_roughness':'CoatRoughness'}
COMPARISON_CAMERAS = {'front':((0,-1,0),(0,0,1)), 'top':((0,0,1),(0,1,0)), 'isometric':((1,1,1),(0,0,1))}


def inspect_render(visual, nodes, settings, path):
    comparison, material = settings['comparison'], settings['material']
    projection = settings.get('projection')
    simple._DisableFirstRenderCameraReset()
    view_type = 'PanoramicProjectionView' if projection else 'ComparativeRenderView' if comparison else 'RenderView'
    resolution = projection['size'] if projection else [800,600]
    view = simple.CreateView(view_type)
    view.UseColorPaletteForBackground = 0
    view.BackgroundColorMode = 'Single Color'
    view.Background = [.09,.10,.12]
    view.OrientationAxesVisibility = 0
    layout = simple.CreateLayout('Inspection')
    layout.AssignView(0,view)
    layout.SetSize(*resolution)
    display = simple.Show(visual,view)
    representation = settings.get('representation')
    measured_representation = None
    if representation:
        association, name = representation['array']
        attributes = visual.PointData if association == 'POINTS' else visual.CellData
        field = attributes.GetArray(name)
        if field is None or field.GetNumberOfComponents() != representation['components']:
            raise ValueError(f'{representation["mode"]} requires {representation["components"]} component(s) in {name}')
        mode, field_property = representation['mode'], representation['property']
        properties(display, {'Representation':mode,field_property:representation['array']})
        if mode == 'Stream Lines':
            properties(display, {'NumberOfAnimationSteps':32})
        measured_representation = {'mode':display.GetProperty('Representation').GetData(),
                                   'array':list(display.GetProperty(field_property))}
    ray_material = settings.get('raytrace')
    ray = enable_ray_material(view,display,ray_material,path.parent) if ray_material else None
    if material is not None:
        properties(display, {'Interpolation':'PBR', **{pv:material[key] for key,pv in MATERIAL_FIELDS.items()}})
    points = comparative_cues(visual,view,{**nodes,'@display':display},comparison) if comparison else [describe_cell(visual)]
    boxes = [row['bounds'] for row in points if row['bounds']]
    if not boxes:
        raise ValueError('No renderable geometry in the ParaView inspection')
    lows = [min(box[2*axis] for box in boxes) for axis in range(3)]
    highs = [max(box[2*axis+1] for box in boxes) for axis in range(3)]
    center = [(a+b)/2 for a,b in zip(lows,highs)]
    size = max(math.dist(lows,highs),1e-9)
    direction, up = COMPARISON_CAMERAS[settings['view']]
    if projection:
        focal = [position+axis*size for position,axis in zip(center,direction)]
        properties(view,{'CameraPosition':center,'CameraFocalPoint':focal,'CameraViewUp':list(up),
                         'ProjectionType':projection['mode'],'ProjectionAngle':projection['angle']})
    else:
        eye = [position+size*2*axis for position,axis in zip(center,direction)]
        properties(view,{'CameraPosition':eye,'CameraFocalPoint':center,'CameraViewUp':list(up),
                         'CameraParallelProjection':1,'CameraParallelScale':size*.62})
    simple.Render(view)
    if not simple.SaveScreenshot(str(path),layout,ImageResolution=resolution) or not path.is_file():
        raise ValueError('ParaView did not write its inspection image')
    measured = ({key: list(display.DiffuseColor) if key=='color' else getattr(display,pv)
                 for key,pv in MATERIAL_FIELDS.items()} if material is not None else None)
    result = {'settings':settings, 'image':path.as_posix(), 'tiles':points, 'material':measured,
              'memory':native_memory(), 'raytrace':ray}
    if measured_representation:
        result['representation'] = measured_representation
    if projection:
        result['projection'] = {'mode':view.GetProperty('ProjectionType').GetData(),
                                'angle':view.GetProperty('ProjectionAngle').GetData(),'size':resolution}
    return result


def enable_ray_material(view, display, settings, folder):
    material_proxy = simple.GetMaterialLibrary()
    if material_proxy is None or 'OSPRay pathtracer' not in view.BackEnd.Available:
        raise ValueError('This ParaView runtime does not include the OSPRay path tracer')
    # Loading the module registers the concrete Python wrapper for GetMaterialLibrary().
    import vtkmodules.vtkRenderingRayTracing

    material_library = material_proxy.SMProxy.GetClientSideObject().GetMaterialLibrary()
    edited = settings['materials']
    name = settings['name']
    document = {'family':'OSPRay','version':'0.0','materials':edited}
    if edited:
        if not material_library.ReadBuffer(json.dumps(document,allow_nan=False)):
            raise ValueError('OSPRay rejected the edited material library')
        material_library.Fire()
    if name == 'None' or name == 'Value Indexed' or name not in display.OSPRayMaterial.Available:
        raise ValueError(f'OSPRay material {name} is unavailable')
    if name in edited:
        for key in edited[name]['textures']:
            texture = material_library.GetTexture(name,key)
            if texture is None:
                raise ValueError(f'OSPRay could not load material texture {key}')
    view.EnableRayTracing = 1
    view.BackEnd = 'OSPRay pathtracer'
    view.SamplesPerPixel = 8
    view.ProgressivePasses = 1
    display.OSPRayMaterial = name
    path = folder/'materials.json'
    path.write_text(json.dumps({**document,'vibeflow_texture_base':'case'},allow_nan=False,indent=2)+'\n',encoding='utf-8')
    return {'name':name,'type':material_library.LookupImplName(name),'backend':view.BackEnd.GetData(),
            'doubles':{key:list(material_library.GetDoubleShaderVariable(name,key))
                       for key in edited.get(name,{}).get('doubles',{})}, 'library':path.as_posix()}


def describe_cell(visual):
    data = visual.GetDataInformation()
    points = data.GetNumberOfPoints()
    return {'points':points,'cells':data.GetNumberOfCells(),'bounds':list(data.GetBounds()) if points else None}


def comparative_cues(visual, view, nodes, comparison):
    columns, rows = comparison['dimensions']
    view.Dimensions = [columns,rows]
    view.OverlayAllComparisons = comparison['overlay']
    timekeeper = simple.GetTimeKeeper()
    time = timekeeper.Time
    times = list(timekeeper.TimestepValues)
    cues = []
    originals = []
    for track in comparison['tracks']:
        target, prop = track['node'], track['property']
        cue = sm.animation.ComparativeAnimationCue()
        cue.Enabled = 1
        if target == '@time':
            if any(value not in times for value in track['values']):
                raise ValueError('Comparative time track must use native data times')
            proxy = None
        else:
            proxy = nodes[target]
            native = proxy.SMProxy.GetProperty(prop)
            if (native is None or native.GetInformationOnly() or native.GetIsInternal()
                    or not (native.IsA('vtkSMDoubleVectorProperty') or native.IsA('vtkSMIntVectorProperty'))
                    or native.GetNumberOfElements() != 1):
                raise ValueError(f'Comparative track requires a scalar numeric property: {prop}')
            originals.append((proxy,prop,getattr(proxy,prop)))
            cue.AnimatedProxy = proxy
            cue.AnimatedPropertyName = prop
            cue.AnimatedElement = 0
        for index,value in enumerate(track['values']):
            cue.SMProxy.UpdateValue(index%columns,index//columns,value)
        cues.append((cue,proxy,track))

    result = []
    for index in range(columns*rows):
        moment = time
        for _,proxy,track in cues:
            value = track['values'][index]
            if proxy is None:
                moment = value
            else:
                properties(proxy,{track['property']:value})
        visual.UpdatePipeline(moment)
        result.append({'x':index%columns,'y':index//columns,'time':moment,**describe_cell(visual)})
    for proxy,prop,value in originals:
        proxy.SetPropertyWithName(prop,value)
    visual.UpdatePipeline(time)
    view.Cues = [cue for cue,_,_ in cues]
    return result


def native_memory():
    from paraview.modules.vtkRemotingCore import vtkPVMemoryUseInformation, vtkPVSystemConfigInformation

    use, capacity = vtkPVMemoryUseInformation(), vtkPVSystemConfigInformation()
    use.CopyFromObject(None)
    capacity.CopyFromObject(None)
    if use.GetSize() != 1 or capacity.GetSize() != 1:
        raise ValueError('ParaView native memory data is unavailable')
    return {'rank': use.GetRank(0), 'process_kib': use.GetProcMemoryUse(0),
            'host_used_kib': use.GetHostMemoryUse(0), 'host_total_kib': capacity.GetHostMemoryTotal(0),
            'process_limit_kib': capacity.GetProcMemoryAvailable(0)}
