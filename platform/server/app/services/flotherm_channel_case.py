"""Generate a steady OpenFOAM conjugate heat transfer case from imported solids."""
from itertools import combinations, product
import json
import math

from .flotherm_coverage import build_coverage_report, source_trace
from .flotherm_flow_devices import (FIXED_LIBRARY, FIXED_MODEL, prepare_devices, source_entries as flow_sources,
                                    topo_set, write_fixed_sources)
from .flotherm_geometry import BOX_UNION, SWEPT_KINDS, cartesian_parts, extrude_plate, point_relation, separated, volume
from .flotherm_merge import merge_boxes, merged_heat_sources, merged_zones
from .flotherm_refinement import refinement_check_dict, refinement_plan
from .flotherm_output import prepare_output, write_manifest
from .flotherm_settings import cht_options, conversion_modes, mesh_skewness
from .flotherm_two_resistance import (LIBRARY, LIBRARY_DIR, axis_and_sign, case_cap, networks,
                                      source_entries, write_native_sources)


APPLICATION = 'chtMultiRegionSimpleFoam'
FLUID = 'fluid'
CP_AIR = 1006.43
INLET, OUTLET = 'ambient_xMin', 'ambient_xMax'
SIDES = ('ambient_yMin', 'ambient_yMax', 'ambient_zMin', 'ambient_zMax')
AMBIENT = (INLET,OUTLET,*SIDES)
# Each block is numbered from its lower z face, counterclockwise from xMin/yMin.
FACE_VERTICES = ((0,4,7,3), (1,2,6,5), (0,1,5,4), (3,7,6,2), (0,3,2,1), (4,5,6,7))
OFFSETS = ((0,0,0), (1,0,0), (1,1,0), (0,1,0), (0,0,1), (1,0,1), (1,1,1), (0,1,1))
PRESSURE = '[1 -1 -2 0 0 0 0]'


def _write(output, path, text):
    target = output / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf8', newline='\n')


def _foam(output, path, text, kind='dictionary'):
    name = path.rsplit('/', 1)[-1]
    _write(output, path, f'FoamFile\n{{ version 2.0; format ascii; class {kind}; object {name}; }}\n{text}\n')


def _vector(values):
    return '(' + ' '.join(map(str, values)) + ')'


def _inside(point, low, high, strict=False):
    return all(a < x < b if strict else a <= x <= b for x, a, b in zip(point, low, high))


def _surface_adjacency(solids, cavities):
    adjacent = {region: set() for region in (FLUID,*cavities,*solids)}
    for name, solid in solids.items():
        surrounding = next((cavity for cavity, row in reversed(tuple(cavities.items()))
                            if all(_inside(p,*row['bounds_m'],strict=True) for p in solid['bounds_m'])), FLUID)
        adjacent[name].add(surrounding)
        adjacent[surrounding].add(name)
    for name in cavities:
        shell = name.replace('cavity','solid',1)
        adjacent[shell].add(name)
        adjacent[name].add(shell)
    return adjacent


def _plate_solid(obj, shape, settings, default_thickness):
    choices = ((obj.get('plate_thickness_override_m'),'objectOverrides.plateThickness'),
               (shape.get('thickness_m'),'source.thickness'),
               (settings.get('thinPlateThickness'),'openfoam.thinPlateThickness'),
               (default_thickness,'mesh-scale-equivalent'))
    thickness, provenance = next((value,source) for value,source in choices if value is not None)
    solid = extrude_plate(shape,thickness)
    return solid,{**obj.get('thin_plate',{}),'plane_reference_m':shape['reference_m'],
                  'normal':shape['normal'],'thickness_m':thickness,'thickness_source':provenance,
                  'volume_m3':volume(solid)}


def _solid_regions(ir, domain, bbox, cell_size, merge=False):
    solids, blockers = {}, []
    spans = [high-low for low, high in zip(domain, domain[3:])]
    mesh_settings = ir['settings'].get('openfoam',{})
    default_thickness = max(.2*cell_size,max(spans)*1e-7)
    for index, obj in enumerate(ir['objects']):
        if 'flow_device' in obj:
            continue
        bounds = bbox(obj)
        shape = obj.get('shape',{})
        device = obj.get('two_resistance') if obj.get('type') == 'twoResistorModel' else None
        plate = None
        if shape.get('kind') == 'flat-plate':
            shape,plate = _plate_solid(obj,shape,mesh_settings,default_thickness)
            bounds = None
            # A flat compact model is located on the PCB-side mounting plane.
            if device and shape['kind'] == 'box':
                shape['origin'] = [value+normal*plate['thickness_m']/2
                                   for value,normal in zip(shape['origin'],device['normal'])]
        if plate or isinstance(shape.get('kind'),str) and shape['kind'] in (BOX_UNION,*SWEPT_KINDS):
            low = shape['origin']
            bounds = [low,[a+b for a,b in zip(low,shape['dimensions'])]]
        material = ir['materials'][obj['material']]
        conductivity = material['conductivity_w_mk']
        density = material.get('density_kg_m3')
        capacity = material.get('specific_heat_j_kg_k')
        if obj['mapping'] not in {'converted', 'equivalent'} or bounds is None:
            reason = obj.get('reason') or '共轭传热需要可验证的闭合实体几何'
        elif (not all(_inside(point, domain[:3], domain[3:], strict=True) for point in bounds)
              or any(b-a <= span*1e-12 for a,b,span in zip(*bounds, spans))):
            reason = '固体需完整位于流道内部，并在三个方向具备可分区的厚度'
        elif any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                 for value in (density,capacity)):
            reason = '共轭传热固体需要正有限的密度和比热'
        else:
            whole_device = bounds
            if device:
                if shape['kind'] != 'box' or axis_and_sign(device['normal']) is None:
                    blockers.append({'object':obj['id'],'reason':'2R 壳体与板侧需要可对齐的主轴平面'})
                    continue
                shape = case_cap(shape,device['normal'],default_thickness)
                low = shape['origin']
                bounds = [low,[a+b for a,b in zip(low,shape['dimensions'])]]
            solid = {'id':obj['id'], 'bounds_m':bounds, 'material':obj['material'],
                     'power_w':obj.get('power_w',0), 'conductivity_w_mk':conductivity,
                     'density_kg_m3':density, 'specific_heat_j_kg_k':capacity, 'shape':shape}
            if 'conductivity_axes' in material:
                solid['conductivity_axes'] = material['conductivity_axes']
            if plate:
                solid['thin_plate'] = plate
            if device:
                solid.update(two_resistance=device,device_bounds_m=whole_device)
            if 'pcb' in obj:
                solid['pcb'] = {**obj['pcb'],'volume_m3':volume(shape)}
                if 'laminate' in material:
                    solid['pcb']['laminate'] = material['laminate']
                elif obj['pcb'].get('model') == 'volume-fraction-laminate':
                    solid['pcb']['model'] = 'supplied-effective-material'
            if shape['kind'] == BOX_UNION:
                parts = cartesian_parts(shape)
                if parts is not None:
                    solid['mesh_boxes'] = parts
                model = obj['type'] if obj.get('type') in ('heatsink','enclosure') else 'compound'
                solid[model] = {**obj.get(model,{}),'volume_m3':volume(shape),'part_count':len(shape['parts'])}
            solids[f'solid{index}'] = solid
            continue
        blockers.append({'object':obj['id'], 'reason':reason})
    tolerance = max(spans)*1e-10
    if merge:
        solids = merge_boxes(solids)
    surface_mesh = any(row['shape']['kind'] != 'box' and 'mesh_boxes' not in row for row in solids.values())
    for left, right in combinations(solids.values(), 2):
        a, b = left['bounds_m'], right['bounds_m']
        gap = max(max(low_a-high_b,low_b-high_a) for low_a,high_a,low_b,high_b in zip(*a,*b))
        if surface_mesh and gap <= tolerance and not separated(left['shape'],right['shape'],tolerance):
            blockers.append({'object':left['id'], 'reason':f'与 {right["id"]} 相交或接触，闭合分区需要明确的流体间隙'})
        elif not surface_mesh and gap < 0 and any(
                all(min(h1,h2)>max(l1,l2) for l1,h1,l2,h2 in zip(*first,*second))
                for first in left.get('mesh_boxes',[a]) for second in right.get('mesh_boxes',[b])):
            blockers.append({'object':left['id'], 'reason':f'固体体积与 {right["id"]} 重叠'})
    return solids, blockers, surface_mesh


def _mesh(output, domain, solids, base_size, mode, background=False, devices=(), cavities=None):
    cavities = cavities or {}
    parts = {name:row.get('mesh_boxes',[row['bounds_m']]) for name,row in solids.items()}
    geometry = [*(box for boxes in parts.values() for box in boxes), *(row['bounds_m'] for row in devices),
                *(row['bounds_m'] for row in cavities.values())]
    axes = [sorted({domain[axis],domain[axis+3],*(point[axis] for box in geometry
             for point in box)}) for axis in range(3)]
    lengths = [[b-a for a,b in zip(coords, coords[1:])] for coords in axes]
    if any(min(parts) <= sum(parts)*1e-12 for parts in lengths):
        raise ValueError('流道存在数值上近重合的几何平面，请对齐相邻坐标')
    ratios = [[length/base_size for length in parts] for parts in lengths]
    if any(value > 200_000 for parts in ratios for value in parts):
        raise ValueError('流道网格超过 200,000 个单元，请增大 baseCellSize')
    counts = [[max(1, math.ceil(value-1e-12)) for value in parts] for parts in ratios]
    for solid in solids.values():
        sheet = solid.get('thin_plate') or solid.get('pcb')
        if sheet:
            axis = max(range(3),key=lambda i:abs(sheet['normal'][i]))
            low, high = solid['bounds_m'][0][axis],solid['bounds_m'][1][axis]
            first,last = axes[axis].index(low),axes[axis].index(high)
            if last-first == 1:
                counts[axis][first] = max(2,counts[axis][first])
    cells = math.prod(map(sum, counts))
    if cells > 200_000:
        raise ValueError('流道网格超过 200,000 个单元，请增大 baseCellSize')
    for row in devices:
        points = [sum(values[coords.index(a):coords.index(b)])
                  for coords, values, a, b in zip(axes, counts, *row['bounds_m'])]
        row['expected_cells'] = math.prod(points)
        if 'face_zone' in row:
            row['expected_faces'] = math.prod(value for i, value in enumerate(points) if i != row['axis'])
    occupancy = {}
    for name, boxes in [*((name,[row['bounds_m']]) for name,row in cavities.items()), *parts.items()]:
        for lo,hi in boxes:
            ranges = [range(coords.index(a),coords.index(b)) for coords,a,b in zip(axes,lo,hi)]
            occupancy.update(dict.fromkeys(product(*ranges),name))
    sizes = tuple(map(len, lengths))
    vertices = {point: index for index,point in enumerate(product(*axes))}
    faces = {name: [] for name in AMBIENT}
    adjacent = {name: set() for name in (FLUID,*cavities,*solids)}
    blocks = []
    total = dict.fromkeys(adjacent, 0)
    for index in product(*(range(size) for size in sizes)):
        zone = occupancy.get(index, FLUID)
        nodes = [vertices[tuple(coords[i+off] for coords,i,off in zip(axes,index,offset))]
                 for offset in OFFSETS]
        subdivisions = [values[i] for i,values in zip(index,counts)]
        total[zone] += math.prod(subdivisions)
        zone_entry = '' if background else zone+' '
        blocks.append(f'hex {_vector(nodes)} {zone_entry}{_vector(subdivisions)} simpleGrading (1 1 1)')
        for axis in range(3):
            for side, delta in enumerate((-1,1)):
                neighbor = list(index)
                neighbor[axis] += delta
                if 0 <= neighbor[axis] < sizes[axis]:
                    other = occupancy.get(tuple(neighbor),FLUID)
                    if other != zone:
                        adjacent[zone].add(other)
                else:
                    face = 2*axis+side
                    name = AMBIENT[face]
                    faces[name].append(_vector(nodes[i] for i in FACE_VERTICES[face]))
    boundary = [f'{name} {{ type {"patch" if mode == "open" or name in (INLET,OUTLET) else "wall"}; '
                f'faces ( {" ".join(rows)} ); }}' for name,rows in faces.items()]
    points = '\n'.join(map(_vector, vertices))
    _foam(output, 'system/blockMeshDict', 'scale 1;\nvertices (\n'+points+'\n);\nblocks (\n'+
          '\n'.join(blocks)+'\n);\nboundary (\n'+'\n'.join(boundary)+'\n);')
    return adjacent, total


def _field(output, region, name, dimensions, value, patches, vector=False):
    body = '\n'.join(f'{patch} {{ {entry} }}' for patch,entry in patches.items())
    _foam(output, f'0/{region}/{name}', f'dimensions {dimensions};\ninternalField uniform {value};\n'
          f'boundaryField\n{{\n{body}\n}}', 'volVectorField' if vector else 'volScalarField')


def _coupled(patches, method):
    return {name: 'type compressible::turbulentTemperatureRadCoupledMixed; Tnbr T; '
            f'kappaMethod {method}; qrNbr none; qr none; value $internalField;' for name in patches}


def _turbulence_fields(output, region, mode, walls):
    entries = {'k':('[0 2 -2 0 0 0 0]',.01,'kqRWallFunction'),
               'epsilon':('[0 2 -3 0 0 0 0]',.1,'epsilonWallFunction'),
               'nut':('[0 2 -1 0 0 0 0]',0,'nutkWallFunction'),
               'alphat':('[1 -1 -1 0 0 0 0]',0,'compressible::alphatWallFunction')}
    for name, (dimensions,value,wall) in entries.items():
        patches = dict.fromkeys(walls,f'type {wall}; value $internalField;')
        if name == 'alphat':
            patches = dict.fromkeys(walls,f'type {wall}; Prt 0.85; value $internalField;')
        if name in ('k','epsilon'):
            entries_ambient = dict.fromkeys(AMBIENT,f'type inletOutlet; inletValue uniform {value}; value $internalField;')
            if mode == 'channel':
                entries_ambient.update(dict.fromkeys(SIDES,'type zeroGradient;'))
                entries_ambient[INLET] = f'type fixedValue; value uniform {value};'
        else:
            entries_ambient = dict.fromkeys(AMBIENT,'type calculated; value $internalField;')
        _field(output,region,name,dimensions,value,{**(entries_ambient if region==FLUID else {}),**patches})


def _fluid_boundaries(output, region, patches, temperature, options):
    if region != FLUID:
        _field(output,region,'U','[0 1 -1 0 0 0 0]','(0 0 0)',dict.fromkeys(patches,'type noSlip;'),vector=True)
        _field(output,region,'T','[0 0 0 1 0 0 0]',temperature,_coupled(patches,'fluidThermo'))
        _field(output,region,'p_rgh',PRESSURE,101325,
               dict.fromkeys(patches,'type fixedFluxPressure; value $internalField;'))
        return {}
    open_mode = options.mode == 'open'
    speed = _vector((options.inlet_velocity or 0,0,0))
    velocity = dict.fromkeys(AMBIENT,'type pressureInletOutletVelocity; value $internalField;')
    thermal = dict.fromkeys(AMBIENT,f'type inletOutlet; inletValue uniform {temperature}; value $internalField;')
    pressure = dict.fromkeys(AMBIENT,'type calculated; value $internalField;')
    reduced_pressure = dict.fromkeys(AMBIENT,'type fixedValue; value uniform 101325;')
    flux_pressure = 'type fixedFluxPressure; value $internalField;'
    if open_mode:
        reduced_pressure = dict.fromkeys(AMBIENT,'type totalPressure; p0 uniform 101325; value $internalField;')
    else:
        velocity[INLET] = f'type fixedValue; value uniform {speed};'
        velocity.update(dict.fromkeys(SIDES,'type slip;'))
        thermal[INLET] = f'type fixedValue; value uniform {temperature};'
        thermal.update(dict.fromkeys(SIDES,'type zeroGradient;'))
        pressure = dict.fromkeys((INLET,*SIDES),'type zeroGradient;')
        pressure[OUTLET] = ('type calculated; value $internalField;' if any(options.gravity)
                            else reduced_pressure[OUTLET])
        reduced_pressure = {**dict.fromkeys((INLET,*SIDES,*patches),flux_pressure),
                            OUTLET:reduced_pressure[OUTLET]}
    _field(output, region, 'U', '[0 1 -1 0 0 0 0]', speed,
           {**velocity,**dict.fromkeys(patches,'type noSlip;')}, vector=True)
    _field(output, region, 'T', '[0 0 0 1 0 0 0]', temperature, {**thermal,**_coupled(patches,'fluidThermo')})
    _field(output, region, 'p_rgh', PRESSURE, 101325, {**reduced_pressure,**dict.fromkeys(patches,flux_pressure)})
    return pressure


def _region_fields(output, region, neighbors, temperature, options, solid, surface_mesh=False, network_terms=(),
                   cavity_coupling=False, refined_mesh=False):
    patches = [f'{region}_to_{other}' for other in sorted(neighbors)]
    fields = dict.fromkeys(patches, 'type zeroGradient;')
    axes = ''
    schemes = ''
    if solid:
        conductivity = solid['conductivity_w_mk']
        transport = 'constIso'
        method = 'solidThermo'
        if isinstance(conductivity,list):
            conductivity = _vector(conductivity)
            transport = 'constAnIso'
            method = 'directionalSolidThermo; alphaAni Anialpha'
            x,y,_ = solid.get('conductivity_axes',((1,0,0),(0,1,0),(0,0,1)))
            axes = f'\ncoordinateSystem {{ origin (0 0 0); e1 {_vector(x)}; e2 {_vector(y)}; }}'
        thermo = f'type heSolidThermo; transport {transport}; equationOfState rhoConst;'
        density = solid['density_kg_m3']
        capacity = solid['specific_heat_j_kg_k']
        mixture = (f'specie {{ molWeight 1; }} thermodynamics {{ Cp {capacity}; Hf 0; Sf 0; }} '
                   f'transport {{ kappa {conductivity}; }} equationOfState {{ rho {density}; }}')
        _field(output, region, 'T', '[0 0 0 1 0 0 0]', temperature, _coupled(patches, method))
        entries = list(network_terms)
        if 'merged_boxes' in solid:
            entries.extend(merged_heat_sources(solid['merged_boxes']['members']))
        elif solid['power_w'] and 'two_resistance' not in solid:
            entries.insert(0,'heatSource { type scalarSemiImplicitSource; '
                           f'selectionMode all; volumeMode absolute; injectionRateSuSp {{ h ({solid["power_w"]} 0); }} }}')
        if entries:
            _foam(output,f'constant/{region}/fvOptions','\n'.join(entries))
        div = ''
        solver = 'h { solver PCG; preconditioner DIC; tolerance 1e-10; relTol .001; }'
        relaxation = 'equations { h 0.7; }' if surface_mesh else 'equations { h 0.95; }'
        fine_solid = refined_mesh or surface_mesh and solid['shape']['kind'] == BOX_UNION
        if cavity_coupling or fine_solid:
            relaxation = 'equations { h 1; }'
        if fine_solid:
            # Small, highly conducting cells otherwise retain heat through diagonal relaxation.
            solver = 'h { solver PCG; preconditioner DIC; tolerance 1e-13; relTol 0; }'
    else:
        thermo = 'type heRhoThermo; transport const; equationOfState perfectGas;'
        mixture = (f'specie {{ molWeight 28.966; }} thermodynamics {{ Cp {CP_AIR}; Hf 0; }} '
                   'transport { mu 1.846e-05; Pr 0.706414; }')
        _foam(output, f'constant/{region}/radiationProperties', 'radiationModel none;')
        _foam(output, 'constant/g',f'dimensions [0 1 -2 0 0 0 0];\nvalue {_vector(options.gravity)};',
              'uniformDimensionedVectorField')
        fields.update(_fluid_boundaries(output,region,patches,temperature,options))
        div = (' div(phi,U) bounded Gauss upwind; div(phi,h) Gauss upwind; div(phi,K) Gauss upwind; '
               'div(phid,p) Gauss upwind; div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;')
        solver = ('p_rgh { solver PCG; preconditioner DIC; tolerance 1e-8; relTol 0.005; } '
                  '"(U|h)" { solver PBiCGStab; preconditioner DILU; tolerance 1e-9; relTol .005; }')
        relaxation = 'fields { p_rgh .7; rho 1; } equations { U .4; h .85; }'
        model = 'simulationType laminar;'
        if options.turbulence == 'realizableKE':
            model = 'simulationType RAS;\nRAS { RASModel realizableKE; turbulence on; printCoeffs on; }'
            _turbulence_fields(output,region,options.mode,patches)
            div += ' div(phi,k) bounded Gauss upwind; div(phi,epsilon) bounded Gauss upwind;'
            solver = solver.replace('(U|h)','(U|h|k|epsilon)')
            relaxation = 'fields { p_rgh .5; rho 1; } equations { U .35; h .75; k .4; epsilon .4; }'
            schemes = '\nwallDist { method meshWave; }'
        if options.mode == 'open':
            solver = solver.replace('tolerance 1e-8; relTol 0.005;', 'tolerance 1e-10; relTol 0;')
            relaxation = ('fields { p_rgh .35; rho 1; } equations { U .25; h .65; '
                          + ('k .4; epsilon .4; ' if options.turbulence == 'realizableKE' else '') + '}')
        _foam(output, f'constant/{region}/turbulenceProperties',model)
        if cavity_coupling:
            relaxation = relaxation.replace('h .85;','h 1;').replace('h .75;','h 1;').replace('h .65;','h 1;')
    _field(output, region, 'p', PRESSURE, 101325, fields)
    _foam(output, f'constant/{region}/thermophysicalProperties', f'thermoType {{ {thermo} mixture pureMixture; '
          f'thermo hConst; specie specie; energy sensibleEnthalpy; }}\nmixture {{ {mixture} }}{axes}')
    discretization = 'limited 0.33' if surface_mesh else 'orthogonal'
    _foam(output, f'system/{region}/fvSchemes', 'ddtSchemes { default steadyState; }\n'
          f'gradSchemes {{ default Gauss linear; }}\ndivSchemes {{ default none;{div} }}\n'
          f'laplacianSchemes {{ default Gauss linear {discretization}; }}\ninterpolationSchemes {{ default linear; }}\n'
          f'snGradSchemes {{ default {discretization}; }}\nfluxRequired {{ default no; p_rgh; }}'+schemes)
    reference = ' pRefCell 0; pRefValue 101325;' if not solid and region != FLUID else ''
    _foam(output, f'system/{region}/fvSolution', f'solvers {{ {solver} }}\n'
          f'SIMPLE {{ nNonOrthogonalCorrectors {int(surface_mesh)};{reference} }}\nrelaxationFactors {{ {relaxation} }}')


def _function(name, spec):
    return f'{name} {{ {spec} executeControl writeTime; writeControl writeTime; }}\n'


def _monitors(ir, domain, solids, options, wall_patches, cavities):
    fields, results, coverage, blockers = [], [], [], []
    field_lib = 'libs (fieldFunctionObjects);'
    for region in (FLUID,*cavities,*solids):
        fields.append(_function(f'temperature_{region}',f'type fieldMinMax; {field_lib} region {region}; '
                      'fields (T); location false;'))
    if options.mode == 'open':
        for patch in AMBIENT:
            fields.append(_function(patch+'Flow',f'type surfaceFieldValue; {field_lib} region {FLUID}; '
                          f'regionType patch; name {patch}; writeFields false; operation sum; fields (phi);'))
    elif options.monitor_flow or any(row['type'] == 'channel-flow' for row in ir['monitors']):
        for label, patch in [('inlet',INLET), ('outlet',OUTLET)]:
            config = f'type surfaceFieldValue; {field_lib} region {FLUID}; regionType patch; name {patch}; writeFields false;'
            fields.append(_function(label+'Flow',config+' operation sum; fields (phi);'))
            fields.append(_function(label+'Temperature',config+' operation weightedAverage; weightField phi; fields (T);'))
    if wall_patches:
        fields.append(_function('channelWallHeat',f'type wallHeatFlux; {field_lib} region {FLUID}; '
                      f'patches {_vector(wall_patches)}; useNamePrefix false; writePrecision 12;'))
    by_id = {}
    for region,solid in solids.items():
        if 'merged_boxes' in solid:
            by_id.update({row['id']:(region,row['cell_zone']) for row in solid['merged_boxes']['members']})
        else:
            by_id[solid['id']] = (region,None)
    for index, monitor in enumerate(ir['monitors']):
        kind = monitor['type']
        name = f'monitor_{index}_{kind.replace("-", "_")}'
        common = {'monitor':index, 'objects':monitor['objects']}
        trace = {key:monitor[key] for key in ('id','name','path','sources')} if 'sources' in monitor else {}
        if kind == 'probe':
            grouped = {}
            for point in monitor.get('locations', []):
                if (not isinstance(point, list) or len(point) != 3 or
                    any(type(x) not in (float,int) or not math.isfinite(x) for x in point) or
                    not _inside(point, domain[:3], domain[3:], strict=True)):
                    break
                relations = [(region,point_relation(solid['shape'],point)) for region,solid in solids.items()]
                owners = [region for region,relation in relations if relation >= 0]
                if len(owners) > 1 or any(relation == 0 for _,relation in relations):
                    break
                cavity = next((name for name,row in reversed(tuple(cavities.items()))
                               if _inside(point,*row['bounds_m'],strict=True)),FLUID)
                grouped.setdefault(owners[0] if owners else cavity,[]).append(point)
            else:
                if grouped:
                    functions = []
                    for region,points in grouped.items():
                        function = name if len(grouped) == 1 else f'{name}_{region}'
                        fields.append(_function(function,f'type probes; libs (sampling); region {region}; '
                                      f'fields (T); probeLocations {_vector(map(_vector,points))};'))
                        results.append({**common, **trace, 'region':region, 'locations':points,
                                        'method':'probes', 'object_name':function})
                        functions.append(function)
                    if trace:
                        coverage.append({**trace, 'mapping':'converted', 'function':', '.join(functions),
                                         'region':next(iter(grouped))})
                    continue
            reason = '探针需要位于计算域内部的唯一网格区域，且与区域交界面分离'
            blockers.append({'object':name, 'reason':reason})
            if trace:
                coverage.append({**trace, 'mapping':'unsupported', 'reason':reason})
        elif kind == 'region-temperature':
            for obj in dict.fromkeys(monitor['objects']):
                if obj not in by_id:
                    blockers.append({'object':name, 'reason':f'{obj} 缺少可求解的固体区域'})
                    continue
                region, zone = by_id[obj]
                function = f'{name}_{region}_{zone}' if zone else f'{name}_{region}'
                selection = f'cellZone; name {zone};' if zone else 'all;'
                fields.append(_function(function,f'type volFieldValue; {field_lib} region {region}; '
                              f'regionType {selection} operation volAverage; fields (T); writeFields false;'))
                results.append({**common, 'object':obj, 'region':region, 'method':'volFieldValue', 'object_name':function,
                                **({'cell_zone':zone} if zone else {})})
        elif kind == 'channel-flow' and options.mode == 'open':
            blockers.append({'object':name, 'reason':'开放边界没有固定的流道入口和出口'})
        else:
            function = 'channelWallHeat' if kind == 'wall-heat-flux' else 'inletFlow'
            results.append({**common, 'region':FLUID, 'method':kind, 'object_name':function,
                            **({'command':'flow'} if options.mode == 'channel' else {})})
    return fields, results, coverage, blockers


_MESH_RUNNER = '''import json
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
gate_path = root / 'mesh_gate.json'
preflight = json.loads((root / 'preflight.json').read_text(encoding='utf8'))
if not preflight['ok']:
    sys.exit('FloTHERM preflight has unresolved blockers. See preflight.json.')
manifest = json.loads((root / 'case-manifest.json').read_text(encoding='utf8'))
regions = [manifest['fluid_region'], @@CAVITIES@@*manifest['solid_regions']]
if sys.argv[1:] == ['--require-ready']:
    gate = json.loads(gate_path.read_text(encoding='utf8'))
    if not gate.get('ok') or gate.get('regions') != regions or any(
            not (root / 'constant' / region / 'polyMesh' / 'boundary').is_file() for region in regions):
        sys.exit('Run Allrun.pre and pass checkMesh for all regions before solving.')
    sys.exit(0)


def record(result):
    pending = gate_path.with_suffix('.json.tmp')
    pending.write_text(json.dumps(result, indent=2) + '\\n', encoding='utf8')
    pending.replace(gate_path)


@@MERGED_INIT@@record({'ok': False, 'regions': regions, 'reason': 'Preparing channel meshes'})
for command in [@@BLOCK@@, @@SURFACE@@@@REFINE_CMD@@@@SPLIT@@@@MERGED_CMD@@@@FLOW_ZONES@@('checkMesh', '-allRegions')]:
    name = command[0]@@REFINE_NAME@@@@MERGED_NAME@@
    log = root / f'log.{name}'
    with log.open('wb') as stream:
        result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        sys.exit(f'{name} failed with status {result.returncode}. See {log.name}.')
@@SNAP_CHECK@@@@REFINE_CHECK@@@@MERGED_CHECK@@@@FLOW_CHECK@@    print(f'{name} finished. See {log.name}.', flush=True)
data = log.read_text(encoding='utf8', errors='replace')
if data.count('Mesh OK.') != len(regions) or re.search(r'Failed \\d+ mesh checks|FOAM FATAL', data):
    sys.exit('checkMesh did not pass for every region. See log.checkMesh.')
@@GATE@@record({'ok': True, 'regions': regions, 'log': 'log.checkMesh'@@MEASURES@@})
'''


_SURFACE_GATE = '''region_stats = {}
for section in re.split(r'(?m)^Mesh stats\\s+', data)[1:]:
    name = section.split()[0]
    cells = re.search(r'(?m)^\\s*cells:\\s+(\\d+)\\s*$', section)
    volume = re.search(r'Total volume = ([^\\s]+)\\.\\s+Cell volumes OK', section)
    if cells and volume:
        region_stats[name] = {'cells': int(cells[1]), 'volume_m3': float(volume[1])}
if set(region_stats) != set(regions) or any(row['cells'] <= 0 or not 0 < row['volume_m3'] < float('inf')
                                           for row in region_stats.values()):
    sys.exit('checkMesh did not report a non-empty finite mesh for every region.')
for region, expected in manifest['mesh_patches'].items():
    boundary = root / 'constant' / region / 'polyMesh' / 'boundary'
    text = boundary.read_text(encoding='utf8')
    counts = {name: int(count) for name, count in re.findall(
        r'(?m)^\\s*(\\w+)\\s*\\{[^}]*?\\bnFaces\\s+(\\d+)\\s*;', text)}
    actual = {name for name, count in counts.items() if count > 0}
    if set(expected) != actual or set(expected) != set(counts):
        sys.exit(f'{region} mesh patches {sorted(counts)} differ from expected {expected}.')
'''

_SNAP_CHECK = '''    if name == 'snappyHexMesh' and 'Finished meshing without any errors' not in log.read_text(encoding='utf8', errors='replace'):
        sys.exit('snappyHexMesh did not meet mesh quality limits. See log.snappyHexMesh.')
'''

_REFINE_CHECK = '''    if name == 'refinementCheck':
        report = log.read_text(encoding='utf8', errors='replace')
        refine_counts = {}
        for name_in_log, count in re.findall(r'cellSet (\\w+) now size (\\d+)', report):
            refine_counts.setdefault(name_in_log, []).append(int(count))
        refinement = {}
        for row in manifest['refinements']:
            counts = refine_counts.get(row['name'], [])
            if len(counts) != 2 or not counts[0] or counts[0] != counts[1]:
                reason = f'Refinement {row["id"]} has mesh counts {counts} at target level {row["level"]}.'
                record({'ok': False, 'regions': regions, 'reason': reason, 'log': log.name})
                sys.exit(reason)
            refinement[row['name']] = {'id': row['id'], 'cells': counts[0], 'minimum_level': row['level']}
'''

_REFINE_GATE = '''if sum(row['cells'] for row in region_stats.values()) > 200000:
    sys.exit('Refined mesh exceeds the 200,000 cell case limit.')
'''

_SKEW_GATE = '''skewness = [float(value) for value in re.findall(r'Max skewness = ([0-9.eE+-]+)', data)]
if len(skewness) != len(regions) or max(skewness) > manifest['max_skewness']:
    sys.exit(f'Actual mesh skewness {skewness} exceeds the requested maximum {manifest["max_skewness"]}.')
'''

_MERGED_CHECK = '''    if name.startswith('mergedSources.'):
        region = command[2]
        counts = {zone: int(count) for zone,count in re.findall(
            r'cellZoneSet (\\w+) now size (\\d+)', log.read_text(encoding='utf8', errors='replace'))}
        for row in manifest['merged_solid_regions'][region]['members']:
            if not counts.get(row['cell_zone'], 0):
                sys.exit(f'{region}/{row["cell_zone"]} has no cells for merged object {row["id"]}.')
        merged_counts[region] = counts
'''

_FLOW_CHECK = '''    if name == 'topoSet':
        report = log.read_text(encoding='utf8', errors='replace')
        zone_counts = {name: int(count) for name, count in re.findall(
            r'(?:cellZoneSet|faceZoneSet) (\\w+) now size (\\d+)', report)}
        for row in manifest['flow_devices']:
            for zone, count in (('cell_zone', 'expected_cells'), ('face_zone', 'expected_faces')):
                if zone in row and zone_counts.get(row[zone]) != row[count]:
                    sys.exit(f'{row[zone]} size {zone_counts.get(row[zone])} differs from expected {row[count]}.')
'''


def generate_channel_case(ir, output, domain, temperature, bbox):
    settings = ir['settings']
    physics = cht_options(settings)
    start_from, merge = conversion_modes(settings)
    count = physics.parallel_subdomains
    cell_size = physics.base_cell_size or min(b-a for a,b in zip(domain,domain[3:]))/10
    solids, blockers, surface_mesh = _solid_regions(ir,domain,bbox,cell_size,merge)
    merged = {name:row['merged_boxes'] for name,row in solids.items() if 'merged_boxes' in row}
    cavities = {name.replace('solid','cavity',1):{
                    'enclosure':row['id'],'bounds_m':row['enclosure']['cavity_bounds_m']}
                for name,row in solids.items() if 'enclosure' in row}
    # Build outer cavities first. An inner enclosure replaces its containing fluid.
    cavities = dict(sorted(cavities.items(),key=lambda pair: -math.prod(
        b-a for a,b in zip(*pair[1]['bounds_m']))))
    two_r, network_blockers = networks(solids)
    blockers.extend(network_blockers)
    devices, flow_blockers = prepare_devices(ir,domain,solids,cell_size,surface_mesh)
    blockers.extend(flow_blockers)
    refinements, refinement_blockers = refinement_plan(ir,domain,solids,devices,surface_mesh)
    blockers.extend(refinement_blockers)
    max_skewness = mesh_skewness(settings)
    fluid_only = devices and not solids
    refinement_dir = f'system/{FLUID}' if fluid_only else 'system'
    refinement_check_path = f'{refinement_dir}/refinementCheckDict'
    prepare_output(output, force=False)
    results, coverage = [], []
    sources, totals, required, walls = [], {}, [], []
    surface_details, mesh_patches = {}, {}
    refinement = settings.get('openfoam',{}).get('surfaceRefinementLevel',[1,2])
    region_gate = surface_mesh or refinements or max_skewness is not None or any(
        'mesh_boxes' in row for row in solids.values())
    if not blockers:
        if surface_mesh:
            from .flotherm_snappy import meshing_case

            _mesh(output,domain,{},cell_size,physics.mode,background=True)
            config, surface_details = meshing_case(output,domain,solids,cell_size,refinement,refinements,max_skewness,
                                                   cavities=cavities)
            _foam(output,'system/snappyHexMeshDict',config)
            adjacent = _surface_adjacency(solids,cavities)
        else:
            adjacent, totals = _mesh(output,domain,solids,cell_size,physics.mode,devices=devices,cavities=cavities)
            if refinements:
                from .flotherm_snappy import volume_only_case

                _foam(output,f'{refinement_dir}/snappyHexMeshDict',volume_only_case(domain,refinements,max_skewness))
        if refinements:
            _foam(output,refinement_check_path,refinement_check_dict(refinements))
        if region_gate:
            mesh_patches = {region: [*(AMBIENT if region==FLUID else ()),
                                    *(f'{region}_to_{other}' for other in sorted(neighbors))]
                            for region,neighbors in adjacent.items()}
        walls = [f'{FLUID}_to_{name}' for name in sorted(adjacent[FLUID])]
        fields, results, coverage, monitor_blockers = _monitors(ir,domain,solids,physics,walls,cavities)
        blockers.extend(monitor_blockers)
        _foam(output,'constant/regionProperties',f'regions (fluid {_vector((FLUID,*cavities))} solid {_vector(solids)});')
        for region, neighbors in adjacent.items():
            solid = solids.get(region)
            _region_fields(output,region,neighbors,temperature,physics,solid,surface_mesh or bool(refinements),
                           source_entries(two_r,region),
                           cavity_coupling=bool(cavities),refined_mesh=bool(refinements))
            if region in merged:
                members = merged[region]['members']
                _foam(output,f'system/{region}/mergedSourcesDict',merged_zones(members))
                sources.extend({'id':row['id'],'region':region,'zone':row['cell_zone'],
                                'power_w':row['power_w'],'field':'h'} for row in members if row['power_w'])
            elif solid and solid['power_w']:
                sources.append({'id':solid['id'], 'region':region, 'power_w':solid['power_w'], 'field':'h',
                                **({'method':'two-resistance'} if 'two_resistance' in solid else {})})
        if devices:
            _foam(output,f'system/{FLUID}/topoSetDict',topo_set(devices))
            _foam(output,f'constant/{FLUID}/fvOptions',flow_sources(devices))
            for device in devices:
                if 'face_zone' in device:
                    fields.append(_function(device['cell_zone']+'Flux',
                        f'type surfaceFieldValue; libs (fieldFunctionObjects); region {FLUID}; '
                        f'regionType faceZone; name {device["face_zone"]}; operation sum; fields (phi); '
                        'writeFields false;'))
        libraries = []
        if any(device['model'] == FIXED_MODEL for device in devices):
            write_fixed_sources(output)
            libraries.append(f'"$FOAM_CASE/{LIBRARY_DIR}/{FIXED_LIBRARY}"')
        if two_r:
            write_native_sources(output)
            libraries.append(f'"$FOAM_CASE/{LIBRARY_DIR}/{LIBRARY}"')
            for device in two_r:
                for branch in ('case','board'):
                    fields.append(_function(device[branch+'_temperature_function'],
                        f'type volFieldValue; libs (fieldFunctionObjects); region {device[branch+"_region"]}; '
                        'regionType all; operation volAverage; fields (T); writeFields false;'))
        _foam(output,'system/controlDict',f'application {APPLICATION}; startFrom {start_from}; startTime 0; '
              f'stopAt endTime; endTime {physics.end_time}; deltaT 1; writeControl timeStep; '
              f'writeInterval {physics.write_interval}; '
              f'writeFormat {settings.get("writeFormat", "ascii")}; writePrecision 12; '
              'runTimeModifiable false;\n'+(f'libs {_vector(libraries)};\n' if libraries else '')+
              'functions {\n'+''.join(fields)+'}\n')
        _foam(output,'system/fvSolution','SIMPLE { energyCoupling { iterations 24; } }' if cavities else 'SIMPLE { }')
        _foam(output,'system/fvSchemes','ddtSchemes { default steadyState; }\n'
              'gradSchemes { default Gauss linear; }\ndivSchemes { default none; }\n'
              'laplacianSchemes { default Gauss linear orthogonal; }\n'
              'interpolationSchemes { default linear; }\nsnGradSchemes { default orthogonal; }')
        method = 'scotch' if settings.get('scotch', True) else 'simple'
        decomposition = f'numberOfSubdomains {count}; method {method}; simpleCoeffs {{ n ({count} 1 1); }}'
        for directory in ('system',*(f'system/{region}' for region in adjacent)):
            _foam(output,f'{directory}/decomposeParDict',decomposition)
        required = sorted(path.relative_to(output).as_posix() for path in output.rglob('*')
                          if path.is_file() and not path.name.startswith('.'))
    region_flag = ", '-region', 'fluid'" if fluid_only else ''
    snappy = f"('snappyHexMesh', '-overwrite'{region_flag}), " if refinements else (
        "('snappyHexMesh', '-overwrite'), " if surface_mesh else '')
    runner = _MESH_RUNNER.replace('@@SURFACE@@',snappy)
    runner = runner.replace('@@REFINE_CMD@@',
        f"('topoSet', '-time', '0'{region_flag}, '-dict', 'system/refinementCheckDict'), " if refinements else '')
    runner = runner.replace('@@REFINE_NAME@@',
        "\n    if command[-1] == 'system/refinementCheckDict':\n        name = 'refinementCheck'" if refinements else '')
    runner = runner.replace('@@REFINE_CHECK@@',_REFINE_CHECK if refinements else '')
    runner = runner.replace('@@CAVITIES@@', "*manifest['sealed_cavities'], " if cavities else '')
    runner = runner.replace('@@BLOCK@@', "('blockMesh', '-region', 'fluid', '-dict', 'system/blockMeshDict')"
                            if fluid_only else "('blockMesh',)")
    runner = runner.replace('@@SPLIT@@', '' if fluid_only else "('splitMeshRegions', '-cellZonesOnly', '-overwrite'), ")
    runner = runner.replace('@@MERGED_INIT@@','merged_counts = {}\n' if merged else '')
    runner = runner.replace('@@MERGED_CMD@@',
        "*(('topoSet', '-region', region, '-dict', 'system/mergedSourcesDict') "
        "for region in manifest['merged_solid_regions']), " if merged else '')
    runner = runner.replace('@@MERGED_NAME@@',
        "\n    if command[-1] == 'system/mergedSourcesDict':\n        name = f'mergedSources.{command[2]}'" if merged else '')
    runner = runner.replace('@@MERGED_CHECK@@',_MERGED_CHECK if merged else '')
    runner = runner.replace('@@FLOW_ZONES@@', "('topoSet', '-region', 'fluid'), " if devices else '')
    flow_check = _FLOW_CHECK
    if refinements:
        flow_check = flow_check.replace('zone_counts.get(row[zone]) != row[count]',
                                       'zone_counts.get(row[zone], 0) < row[count]')
    runner = runner.replace('@@FLOW_CHECK@@', flow_check if devices else '')
    measures = (", 'region_stats': region_stats" if region_gate else '') + (
        ", 'flow_zone_counts': zone_counts" if devices else '')
    if refinements:
        measures += ", 'refinements': refinement"
    if merged:
        measures += ", 'merged_zone_counts': merged_counts"
    if max_skewness is not None:
        measures += ", 'max_skewness': max(skewness)"
    runner = runner.replace('@@MEASURES@@',measures)
    runner = runner.replace('@@SNAP_CHECK@@',_SNAP_CHECK if snappy else '')
    gate = _SURFACE_GATE if region_gate else ''
    if refinements:
        gate += _REFINE_GATE
    if max_skewness is not None:
        gate += _SKEW_GATE
    _write(output,'system/channel_mesh.py',runner.replace('@@GATE@@',gate))
    manifest = {'schema':'vibeflow.flotherm2foam.case-manifest.v1', 'source':ir['source'],
                'application':APPLICATION, 'objects':[obj['id'] for obj in ir['objects']],
                'heat_sources':sources, 'required_overrides':blockers, 'fluid_region':FLUID,
                'solid_regions':{name:{k:v for k,v in row.items() if k not in ('shape','mesh_boxes')}
                                 for name,row in solids.items()},
                'mesh_cells':{} if refinements else totals, 'parallel_subdomains':count,
                'write_format':settings.get('writeFormat','ascii'), 'monitor_results':results,
                'monitor_coverage':coverage}
    if surface_details:
        manifest['mesh_geometry'] = surface_details
    if mesh_patches:
        manifest['mesh_patches'] = mesh_patches
    if cavities:
        manifest['sealed_cavities'] = cavities
    if merge:
        manifest['merged_solid_regions'] = merged
    if start_from=='latestTime':
        manifest['start_from'] = start_from
    if refinements:
        manifest['refinements'] = refinements
        manifest['background_mesh_cells'] = totals
    if max_skewness is not None:
        manifest['max_skewness'] = max_skewness
    if two_r:
        manifest['two_resistance_networks'] = two_r
    if devices:
        manifest['flow_devices'] = devices
    ambient = {'domain_bounds_m':domain, 'reference_temperature_k':temperature,
               'air_cp_j_kg_k':CP_AIR, 'interfaces':walls}
    if physics.mode == 'channel':
        manifest['channel'] = {'inlet':INLET, 'outlet':OUTLET, 'inlet_velocity_m_s':physics.inlet_velocity,
                               **ambient, 'flow_monitors':physics.monitor_flow or any(
                                   monitor['type'] == 'channel-flow' for monitor in ir['monitors'])}
    else:
        manifest['open_boundaries'] = {**ambient,'patches':list(AMBIENT)}
    if physics.mode == 'open' or any(physics.gravity) or physics.turbulence != 'laminar':
        manifest['physics'] = {'boundary_mode':physics.mode, 'turbulence':physics.turbulence,
                               'gravity_m_s2':list(physics.gravity)}
    if ir.get('regions'):
        selected = {row['id']:row['level'] for row in refinements if row['source']=='settings.refinement'}
        manifest['regions'] = [
            {**row,'mapping':'refinement','refinement_level':selected[row['id']]} if row['id'] in selected else
            {**row,'mapping':'domain' if row['type']=='domain' else 'metadata-only'} for row in ir['regions']]
    power = build_coverage_report(ir)['summary']
    trace = source_trace(ir, manifest)
    if trace:
        manifest['source_trace'] = trace
    power['generated_power_w'] = math.fsum(row['power_w'] for row in sources)
    preflight = {'ok':not blockers, 'blockers':blockers, 'required_files':[*required,'system/channel_mesh.py']}
    for name, data in [('case-manifest',manifest), ('required-overrides',blockers), ('power-balance',power),
                       ('preflight',preflight), ('mesh_gate',{'ok':False, 'reason':'等待多区域 checkMesh'})]:
        _write(output,name+'.json',json.dumps(data,indent=2,allow_nan=False)+'\n')
    write_manifest(output,[path.relative_to(output).as_posix() for path in output.rglob('*')
                           if path.is_file() and not path.name.startswith('.')])
    return manifest
