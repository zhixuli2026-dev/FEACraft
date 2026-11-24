"""Emit conformal multi-region meshing dictionaries and closed CAD surfaces."""
from itertools import product
import math

from .flotherm_geometry import cross, point_relation, subtract, surface, volume
from .flotherm_refinement import refinement_entries


def _tuple(values):
    return '('+' '.join(format(value,'.16g') for value in values)+')'


def _write_surface(path, name, triangles):
    with path.open('w',encoding='ascii',newline='\n') as stream:
        stream.write(f'solid {name}\n')
        for a,b,c in triangles:
            normal = cross(subtract(b,a),subtract(c,a))
            length = math.hypot(*normal)
            if not math.isfinite(length) or length <= 0:
                raise ValueError(f'实体 {name} 的三角面退化')
            stream.write('facet normal '+' '.join(format(v/length,'.16g') for v in normal)+'\nouter loop\n')
            for point in (a,b,c):
                stream.write('vertex '+' '.join(format(v,'.16g') for v in point)+'\n')
            stream.write('endloop\nendfacet\n')
        stream.write(f'endsolid {name}\n')


def _fluid_seed(domain, solids):
    # Interior solids leave a neighborhood of each domain corner in the fluid.
    clearance = [min(solid['bounds_m'][0][i]-domain[i] for solid in solids.values()) for i in range(3)]
    point = [a+distance*fraction for a,distance,fraction in zip(domain,clearance,(.41,.53,.67))]
    if any(point_relation(solid['shape'],point) >= 0 for solid in solids.values()):
        raise ValueError('未能确定固体外的背景流体种子点')
    return point


def _cavity_seed(name, cavities, solids):
    low, high = cavities[name]['bounds_m']
    children = [row['bounds_m'] for other, row in cavities.items() if other != name
                and all(a < b <= c < d for a,b,c,d in zip(low,*row['bounds_m'],high))]
    fractions = (.2113, .7887, .3869, .6131, .5)
    for coordinates in product(fractions, repeat=3):
        point = [a+fraction*(b-a) for a,b,fraction in zip(low,high,coordinates)]
        if any(all(a <= p <= b for a,p,b in zip(start,point,end)) for start,end in children):
            continue
        if all(point_relation(solid['shape'], point) < 0 for solid in solids.values()):
            return point
    raise ValueError(f'未能在密闭空腔 {name} 中找到独立的流体种子点')


def meshing_case(output, domain, solids, base_size, refinement, targets=(), max_skewness=None, *, cavities=None):
    folder = output/'constant'/'triSurface'
    folder.mkdir(parents=True)
    definitions, refinement_surfaces, seeds, details = [], [], [], {}
    local_levels = {row['surface']:row['level'] for row in targets if 'surface' in row}
    for name, solid in solids.items():
        shape = solid['shape']
        _, triangles, seed = surface(shape)
        _write_surface(folder/(name+'.stl'),name,triangles)
        low, high = solid['bounds_m']
        if 'min_feature_m' in shape:
            thickness = shape['min_feature_m']
        else:
            thickness = min(math.hypot(*edge) for edge in shape['edges_m']) if 'edges_m' in shape else min(shape['dimensions'])
        target = math.ceil(math.log2(max(1,3*base_size/thickness)))
        if target > 6:
            raise ValueError(f'实体 {name} 尺寸过小，请减小 baseCellSize')
        local = local_levels.get(name,0)
        minimum, maximum = max(refinement[0],target-1,local), max(refinement[1],target,local)
        definitions.append(f'{name}.stl {{ type triSurfaceMesh; name {name}; }}')
        refinement_surfaces.append(f'{name} {{ level ({minimum} {maximum}); }}')
        seeds.append(f'({_tuple(seed)} {name})')
        details[name] = {'kind':shape['kind'],'surface':f'constant/triSurface/{name}.stl',
                         'triangle_count':len(triangles),'bounds_m':low+high,'volume_m3':volume(shape),
                         'refinement':[minimum,maximum]}
    seeds.insert(0,f'({_tuple(_fluid_seed(domain,solids))} fluid)')
    for name in cavities or ():
        seeds.append(f'({_tuple(_cavity_seed(name,cavities,solids))} {name})')
    settings = _mesh_dict(definitions,refinement_surfaces,f'locationsInMesh ( {" ".join(seeds)} );',
                          targets,True,max_skewness)
    return settings, details


def volume_only_case(domain, targets, max_skewness=None):
    point = [a+fraction*(b-a) for a,b,fraction in zip(domain,domain[3:],(.41,.53,.67))]
    return _mesh_dict([],[],f'locationInMesh {_tuple(point)};',targets,False,max_skewness)


def _mesh_dict(definitions, surface_levels, location, targets, snap, max_skewness):
    refinement_geometry, region_levels = refinement_entries(targets)
    regions = ' '+' '.join(region_levels)+' ' if region_levels else ''
    flags = '\nwriteFlags (scalarLevels);' if targets else ''
    boundary_skew = 3 if max_skewness is None else max_skewness
    internal_skew = 2.5 if max_skewness is None else max_skewness
    body = f'''castellatedMesh true;
snap {str(snap).lower()};
addLayers false;
geometry {{ {' '.join([*definitions,*refinement_geometry])} }}
castellatedMeshControls
{{
    maxLocalCells 200000;
    maxGlobalCells 200000;
    minRefinementCells 0;
    nCellsBetweenLevels 2;
    resolveFeatureAngle 30;
    features ();
    refinementSurfaces {{ {' '.join(surface_levels)} }}
    refinementRegions {{{regions}}}
    {location}
    allowFreeStandingZoneFaces true;
}}
snapControls
{{
    nSmoothPatch 10;
    tolerance 1.6;
    nSolveIter 50;
    nRelaxIter 8;
    nFeatureSnapIter 12;
    implicitFeatureSnap true;
    explicitFeatureSnap false;
    multiRegionFeatureSnap true;
}}
addLayersControls {{ layers {{}} }}
meshQualityControls
{{
    maxNonOrtho 70;
    maxBoundarySkewness {boundary_skew};
    maxInternalSkewness {internal_skew};
    maxConcave 80;
    minVol 1e-22;
    minTetQuality 1e-26;
    minArea -1;
    minTwist 0.025;
    minDeterminant 0.001;
    minFaceWeight 0.02;
    minVolRatio 0.01;
    minTriangleTwist -1;
    nSmoothScale 4;
    errorReduction 0.75;
}}
mergeTolerance 1e-8;{flags}'''
    return body
