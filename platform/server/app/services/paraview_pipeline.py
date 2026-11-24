"""Validate and stage declarative ParaView pipelines for the registered executor."""

import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess

from .attempt_workspace import canonical_path, checked_path, MAX_BYTES
from .animation import normalize_camera_track
from .paraview import paraview_executable
from . import paraview_materials


RUNNER = Path(__file__).with_name('paraview_native')/'pipeline.py'
PLUGIN_SUPPORT = RUNNER.with_name('plugin_support.py')
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
PROPERTY_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_ ]{0,95}')
OPERATORS = {
    'sources': frozenset(('ArrowSource', 'ConeSource', 'CubeSource', 'CylinderSource', 'DiskSource',
                          'LineSource', 'PlaneSource', 'PointSource', 'RTAnalyticSource', 'SphereSource')),
    'filters': frozenset(('Append', 'AppendPolyData', 'Calculator', 'CellDataToPointData', 'CleanPolyData',
                          'Clip', 'Contour', 'DataSetSurfaceFilter', 'DataSetTriangleFilter', 'ElevationFilter',
                          'Glyph', 'Gradient', 'GroupDataSets', 'HyperTreeGridToUnstructuredGrid',
                          'PointDataToCellData', 'ResampleWithDataset',
                          'SliceWithPlane', 'SmoothPolyDataFilter', 'StreamTracer', 'TemporalStatistics', 'Threshold', 'TransformFilter',
                          'TriangleFilter', 'WarpScalar', 'WarpVector')),
}
PLUGIN_OPERATORS = {
    'AcceleratedAlgorithms': frozenset(('FlyingEdges2D','FlyingEdges3D','FlyingEdgesPlaneCutter')),
    'ArrowGlyph': frozenset(('ArrowGlyphFilter',)),
    'BagPlotViewsAndFilters': frozenset(('ExtractBagPlots',)),
    'DigitalRockPhysics': frozenset(('AnalyzeMaterialClusters','ExplodeMaterialClusters')),
    'ExplicitStructuredGrid': frozenset(('ExplicitUnstructuredGeneratorSource','ExplicitStructuredGridCrop',
                                         'ExplicitStructuredGridSurfaceFilter','ExplicitStructuredGridToUnstructuredGrid',
                                         'ImageDataToExplicitStructuredGrid')),
    'GeodesicMeasurement': frozenset(('FastMarchingGeodesicDistanceField','GeodesicMeasurement')),
    'HyperTreeGridADR': frozenset(('ResampleToHyperTreeGrid',)),
    'LagrangianParticleTracker': frozenset(('LagrangianParticleTracker',)),
    'Moments': frozenset(('MomentVectors',)),
    'MooseXfemClip': frozenset(('MooseXfemClip',)),
    'NonOrthogonalSource': frozenset(('ShearedWavelet',)),
    'ParametricSurfaces': frozenset(('ParametricSurfaces',)),
    'SLACTools': frozenset(('SamplePlaneSource',)),
    'StreamingParticles': frozenset(('RandomPoints',)),
    'ThickenLayeredCells': frozenset(('ThickenCells',)),
}
PLUGIN_READERS = {
    'AnalyzeNIfTIReaderWriter': {'AnalyzeReader':('.hdr',), 'NIfTIReader':('.nii','.nii.gz')},
    'Datamine': {'DataMineWireFrameReader':('.dm',)},
    'GMVReader': {'GMVSeriesReader':('.gmv',)},
    'LANLX3DReader': {'LANLX3DReader':('.x3d',)},
    'LegacyExodusReader': {'LegacyExodusIIReader':('.e','.ex2','.exo')},
}
PLUGIN_REPRESENTATIONS = {
    'Surface LIC': ('SurfaceLIC','SelectInputVectors',3),
    'Stream Lines': ('StreamLinesRepresentation','InputVectors',3),
    'Bump Mapped Surface': ('EmbossingRepresentations','BumpMapInputDataArray',1),
    'Extrusion Surface': ('EmbossingRepresentations','ExtrusionInputDataArray',1),
}
PANORAMIC_VIEWS = {'Equirectangular': (1024,512), 'Azimuthal (fish eye)': (800,800)}
DATASET_SUFFIXES = frozenset(('.vtk', '.vtp', '.vtu', '.vti', '.vtr', '.vts', '.vtt'))
INFORMATION_MARKER = b'VIBEFLOW_PARAVIEW_INFORMATION '


def dataset_path(value, extensions=DATASET_SUFFIXES):
    path = canonical_path(value)
    if (path.split('/')[0].casefold() == '.vibeflow'
            or not any(path.casefold().endswith(suffix) for suffix in extensions)):
        raise ValueError('Choose a supported case VTK dataset')
    return path


def dataset_information(root, relative):
    path = checked_path(root, relative)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('The selected ParaView dataset is not a supported file size')
    process = subprocess.run([paraview_executable('pvpython'), '-m', 'app.services.result_readers', str(path)],
                             cwd=Path(__file__).resolve().parents[2], timeout=30, capture_output=True)
    records = [row[len(INFORMATION_MARKER):] for row in process.stdout.splitlines()
               if row.startswith(INFORMATION_MARKER)]
    if process.returncode or len(records) != 1:
        raise ValueError('ParaView could not read the selected dataset')
    return json.loads(records[0])


def normalize(nodes, plugin_operators=None, plugin_readers=None):
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 32:
        raise ValueError('A ParaView pipeline requires 1 to 32 nodes')
    if len(json.dumps(nodes, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 128 * 1024:
        raise ValueError('ParaView pipeline exceeds 128 KiB')
    normalized, seen = [], set()
    operators = {group: names | (plugin_operators or {}).get(group,set()) for group,names in OPERATORS.items()}
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {'id','group','name','file','files','inputs','properties','reader'}:
            raise ValueError('Unknown ParaView node fields')
        name, group, identifier = (node.get(key, '') for key in ('name', 'group', 'id'))
        if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in seen):
            raise ValueError('ParaView node IDs must be unique identifiers')
        if not isinstance(group, str) or not isinstance(name, str):
            raise ValueError('Invalid ParaView operator')
        inputs, properties = node.get('inputs', {}), node.get('properties', {})
        if not isinstance(inputs, dict) or not isinstance(properties, dict):
            raise ValueError('ParaView inputs and properties must be objects')
        if any(not PROPERTY_NAME.fullmatch(key) or not isinstance(refs, list) or not refs
               or any(not isinstance(ref, str) or ref not in seen for ref in refs)
               for key, refs in inputs.items()):
            raise ValueError('ParaView connections must refer to earlier nodes')
        if any(not PROPERTY_NAME.fullmatch(key) or key in inputs for key in properties):
            raise ValueError('Invalid ParaView property name')
        if group == 'file':
            if name or inputs or properties or (('file' in node) == ('files' in node)):
                raise ValueError('Choose a supported case VTK dataset')
            if 'file' in node:
                if 'reader' in node:
                    reader = node['reader']
                    installed = plugin_readers or {}
                    if not isinstance(reader,str) or reader not in installed:
                        raise ValueError('Select the installed plugin for this case reader')
                    dataset = {'file':dataset_path(node['file'],installed[reader]), 'reader':reader}
                else:
                    dataset = {'file':dataset_path(node['file'])}
            else:
                files = node['files']
                if 'reader' in node or not isinstance(files, list) or not 2 <= len(files) <= 256:
                    raise ValueError('A ParaView series needs 2 to 256 ordered case files')
                files = [dataset_path(path) for path in files]
                if len(set(files)) != len(files) or len({PurePosixPath(path).suffix.casefold() for path in files}) != 1:
                    raise ValueError('A ParaView series needs distinct files with the same extension')
                dataset = {'files': files}
            normalized.append({'id': identifier, 'group': group, **dataset})
        else:
            if name not in operators.get(group, ()) or {'file', 'files', 'reader'} & node.keys():
                raise ValueError(f'ParaView operator is not available for managed pipelines: {group}/{name}')
            if bool(inputs) != (group == 'filters'):
                raise ValueError('Filters require a connection. Sources cannot have connections.')
            normalized.append({'id': identifier, 'group': group, 'name': name,
                               'inputs': inputs, 'properties': properties})
        seen.add(identifier)
    return normalized


def normalize_animation(value, nodes):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value)-{'fps','tracks','camera','flipbook','streaming'}:
        raise ValueError('Invalid ParaView property animation')
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 128 * 1024:
        raise ValueError('ParaView animation exceeds 128 KiB')
    fps, tracks = value.get('fps', 2), value.get('tracks', [])
    if type(fps) not in (int, float) or not 0.1 <= fps <= 60 or not isinstance(tracks, list) or len(tracks) > 64:
        raise ValueError('ParaView animation requires 0.1–60 FPS and up to 64 tracks')
    if 'streaming' in value:
        updates = value['streaming']
        if (set(value)-{'fps','streaming'} or type(updates) is not int or not 2 <= updates <= 32
                or nodes[-1].get('name') != 'RandomPoints'):
            raise ValueError('Particle streaming requires the Random Points source and 2 to 32 preview steps')
        return {'tracks':[],'camera':None,'fps':fps,'streaming':updates}
    if 'flipbook' in value:
        sequence = value['flipbook']
        identifiers = {node['id'] for node in nodes}
        if (set(value)-{'fps','flipbook'} or not isinstance(sequence,list) or not 2 <= len(sequence) <= 32
                or any(not isinstance(item,str) or item not in identifiers for item in sequence)
                or len(set(sequence)) != len(sequence)):
            raise ValueError('Flip Book requires 2 to 32 distinct pipeline nodes')
        return {'tracks':[],'camera':None,'fps':fps,'times':[index/fps for index in range(len(sequence))],
                'flipbook':sequence}
    identifiers = {'@display', *(node['id'] for node in nodes if node['group'] != 'file')}
    animations, targets, time_bounds = [], set(), []
    for track in tracks:
        if not isinstance(track, dict) or set(track)-{'node', 'property', 'frames', 'interpolation'}:
            raise ValueError('Invalid ParaView property track')
        node, prop = track.get('node'), track.get('property')
        if (not isinstance(node, str) or node not in identifiers or not isinstance(prop, str)
                or not PROPERTY_NAME.fullmatch(prop) or (node,prop) in targets):
            raise ValueError('Each ParaView property track needs one unique node and property')
        targets.add((node,prop))
        interpolation, frames = track.get('interpolation','linear'), track.get('frames')
        if interpolation not in ('linear','smooth','step') or not isinstance(frames,list) or not 2 <= len(frames) <= 64:
            raise ValueError('A track needs 2 to 64 frames and linear, smooth or step interpolation')
        previous = -1.0
        layout = None
        for frame in frames:
            if not isinstance(frame,dict) or set(frame) != {'t','value'}:
                raise ValueError('A ParaView keyframe needs t and value')
            moment, setting = frame['t'], frame['value']
            if type(moment) not in (int,float) or not math.isfinite(moment) or moment < 0 or moment <= previous:
                raise ValueError('Keyframe times must be non-negative and strictly increase')
            previous = moment
            if interpolation != 'step':
                values = setting if isinstance(setting,list) else [setting]
                current = (isinstance(setting,list),len(values))
                if not values or any(type(item) not in (int,float) for item in values) or (layout and current != layout):
                    raise ValueError('An interpolated track requires compatible numeric keyframes')
                layout = current
        animations.append({'node':node, 'property':prop, 'interpolation':interpolation, 'frames':frames})
        time_bounds.append((frames[0]['t'], frames[-1]['t']))
    camera = normalize_camera_track(value['camera']) if 'camera' in value else None
    if camera:
        time_bounds.append((camera['frames'][0]['t'],camera['frames'][-1]['t']))
    if not time_bounds:
        raise ValueError('Choose at least one animated property or camera')
    start, end = min(item[0] for item in time_bounds), max(item[1] for item in time_bounds)
    steps = (end-start)*fps
    if not math.isfinite(steps) or steps > 1000:
        raise ValueError('ParaView animation exceeds 1000 frame intervals')
    times = [start+i/fps for i in range(math.ceil(steps-1e-9))]+[end]
    return {'tracks':animations, 'camera':camera, 'fps':fps, 'times':times}


def normalize_extractors(value, nodes):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError('Choose up to 16 ParaView extractors')
    identifiers = {node['id'] for node in nodes}
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) - {'id', 'node', 'format', 'start', 'end', 'every'}:
            raise ValueError('Unknown ParaView extractor fields')
        identifier, node, kind = (row.get(key) for key in ('id', 'node', 'format'))
        if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier)
                or PureWindowsPath(identifier).is_reserved() or identifier.casefold() in seen
                or not isinstance(node, str) or node not in identifiers
                or kind not in ('VTK', 'CSV', 'PNG')):
            raise ValueError('An extractor needs a unique ID, pipeline node and VTK, CSV or PNG format')
        start, end, every = row.get('start', 0), row.get('end'), row.get('every', 1)
        if (type(start) is not int or not 0 <= start < 1000 or type(every) is not int or not 1 <= every <= 1000
                or (end is not None and (type(end) is not int or not start <= end < 1000))):
            raise ValueError('Invalid ParaView extractor step range or interval')
        seen.add(identifier.casefold())
        result.append({'id': identifier, 'node': node, 'format': kind, 'start': start, 'end': end, 'every': every})
    return result


def normalize_inspection(value, nodes, plugins=()):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'comparison','material','view','raytrace','representation','projection'}:
        raise ValueError('Invalid ParaView inspection parameters')
    camera = value.get('view', 'isometric')
    if camera not in ('front', 'isometric', 'top'):
        raise ValueError('Choose front, isometric or top view')
    material = value.get('material')
    if material is not None:
        defaults = {'color': [.85,.85,.85], 'metallic': 0, 'roughness': .3, 'opacity': 1,
                    'coat_strength': 0, 'coat_roughness': .2}
        if not isinstance(material, dict) or set(material) - defaults.keys():
            raise ValueError('Unknown PBR material field')
        material = defaults | material
        color = material['color']
        if not isinstance(color, list) or len(color) != 3 or any(
                type(x) not in (int,float) or not 0 <= x <= 1 for x in color):
            raise ValueError('PBR color requires three finite values between 0 and 1')
        if any(type(x) not in (int,float) or not 0 <= x <= 1 for k,x in material.items() if k != 'color'):
            raise ValueError('PBR material coefficients must be between 0 and 1')
    comparison = value.get('comparison')
    if comparison is not None:
        comparison = normalize_comparison(comparison, nodes)
    settings = {'comparison': comparison, 'material': material, 'view': camera}
    if 'representation' in value:
        representation = value['representation']
        if not isinstance(representation,dict) or set(representation) != {'mode','array'}:
            raise ValueError('A plugin representation needs a mode and its input array')
        mode, array = representation['mode'], representation['array']
        definition = PLUGIN_REPRESENTATIONS.get(mode) if isinstance(mode,str) else None
        if definition is None or definition[0] not in plugins:
            raise ValueError('Select the installed plugin for this ParaView representation')
        if (not isinstance(array,list) or len(array)!=2 or array[0] not in ('POINTS','CELLS')
                or not isinstance(array[1],str) or not array[1] or len(array[1])>255):
            raise ValueError('Choose a named point or cell array for the ParaView representation')
        if comparison is not None or material is not None or 'raytrace' in value:
            raise ValueError('Plugin representations require a single view')
        _, property_name, components = definition
        settings['representation'] = {**representation,'property':property_name,'components':components}
    if 'projection' in value:
        projection = value['projection']
        if ('PanoramicProjectionView' not in plugins or not isinstance(projection,dict)
                or set(projection) != {'mode','angle'}):
            raise ValueError('A panoramic inspection needs its installed plugin, mode and angle')
        mode, angle = projection['mode'], projection['angle']
        if (not isinstance(mode,str) or mode not in PANORAMIC_VIEWS or type(angle) not in (int,float)
                or not 90 <= angle <= 360):
            raise ValueError('Choose a panoramic mode and an angle from 90 to 360 degrees')
        if comparison is not None or material is not None or 'raytrace' in value or 'representation' in settings:
            raise ValueError('Panoramic inspection requires a single standard surface view')
        settings['projection'] = {**projection,'size':list(PANORAMIC_VIEWS[mode])}
    if 'raytrace' in value:
        if comparison is not None or material is not None:
            raise ValueError('Select a single view and OSPRay shading for the material library')
        settings['raytrace'] = paraview_materials.normalize(value['raytrace'])
    return settings


def normalize_comparison(comparison, nodes):
    if not isinstance(comparison, dict) or set(comparison) - {'dimensions','overlay','tracks'}:
        raise ValueError('Invalid comparative layout')
    size, overlay, tracks = comparison.get('dimensions',[2,2]), comparison.get('overlay',False), comparison.get('tracks')
    if (not isinstance(size,list) or len(size) != 2 or any(type(n) is not int or not 1 <= n <= 4 for n in size)
            or not 2 <= math.prod(size) <= 12 or type(overlay) is not bool
            or not isinstance(tracks,list) or not 1 <= len(tracks) <= 4):
        raise ValueError('Comparative view needs 2 to 12 tiles and 1 to 4 tracks')
    ids = {'@display','@time',*(row['id'] for row in nodes if row['group'] != 'file')}
    axes = set()
    for track in tracks:
        if not isinstance(track,dict) or set(track) != {'node','property','values'}:
            raise ValueError('Comparative tracks require node, property and values')
        target, prop, values = (track[key] for key in ('node','property','values'))
        if (not isinstance(target,str) or target not in ids or not isinstance(prop,str)
                or not PROPERTY_NAME.fullmatch(prop) or (target == '@time' and prop != 'Time')
                or (target,prop) in axes or not isinstance(values,list) or len(values) != math.prod(size)
                or any(type(x) not in (int,float) or not math.isfinite(x) for x in values)):
            raise ValueError('Comparative tracks need unique scalar settings matching the layout')
        axes.add((target,prop))
    return {'dimensions':size,'overlay':overlay,'tracks':tracks}


def normalize_plugins(value):
    if value is None:
        return []
    if (not isinstance(value,list) or len(value)>8 or any(not isinstance(item,str) or not IDENTIFIER.fullmatch(item)
                                                         for item in value) or len(set(value))!=len(value)):
        raise ValueError('ParaView requires up to 8 distinct installed plugin names')
    return value


def stage(nodes, folder, animation=None, extractors=None, inspection=None, plugins=None, volume_format=None):
    payload = json.dumps({'nodes': nodes, 'output': folder, 'animation': animation, 'extractors': extractors,
                          'inspection': inspection, 'plugins':plugins, 'volume_format':volume_format},
                         ensure_ascii=True, allow_nan=False)
    native = RUNNER.read_text(encoding='utf-8')
    if plugins:
        native = PLUGIN_SUPPORT.read_text(encoding='utf-8') + '\n\n' + native
    return (native + '\n\nrun(json.loads(' + repr(payload) + '))\n').encode('utf-8')
