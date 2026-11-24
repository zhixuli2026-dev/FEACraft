"""Parametric FloTHERM solids in global metre coordinates."""
from itertools import combinations, product
import math


POLYHEDRA = {
    'oriented-box': (((0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1)),
                     ((0,1,2,3),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7))),
    'prism': (((0,0,0),(1,0,0),(0,1,0),(0,0,1),(1,0,1),(0,1,1)),
              ((0,1,2),(3,4,5),(0,1,4,3),(1,2,5,4),(2,0,3,5))),
    'tet': (((0,0,0),(1,0,0),(0,1,0),(0,0,1)),((0,1,2),(0,1,3),(0,2,3),(1,2,3))),
}
POLYHEDRA['invertedTet'] = (tuple(tuple(1-value for value in point) for point in POLYHEDRA['tet'][0]),
                            POLYHEDRA['tet'][1])
SWEPT_KINDS = frozenset((*POLYHEDRA,'cylinder'))
SEGMENTS = 48
PLANE_AXES = {'yz':0,'xz':1,'xy':2}
IDENTITY = ((1,0,0),(0,1,0),(0,0,1))
BOX_UNION = 'box-union'


def subtract(a,b):
    return tuple(x-y for x,y in zip(a,b))


def dot(a,b):
    return math.fsum(x*y for x,y in zip(a,b))


def cross(a,b):
    return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])


def transform(origin, axes, point):
    return [a+dot(row,point) for a,row in zip(origin,zip(*axes))]


def _point(value):
    return (isinstance(value,(list,tuple)) and len(value) == 3 and
            all(type(item) in (int,float) and math.isfinite(item) for item in value))


def plane_normal(plane):
    label = plane.strip().lower()
    axis = PLANE_AXES.get(label.removeprefix('+').removeprefix('-'))
    if axis is None:
        raise ValueError(f'平面 {plane!r} 需要 xy、xz 或 yz 方向')
    return [(-1 if label.startswith('-') else 1) if i == axis else 0 for i in range(3)]


def unit_axes(axes):
    if not isinstance(axes,(tuple,list)) or len(axes) != 3 or not all(map(_point,axes)):
        raise ValueError('实体需要三条有限的三维边向量')
    lengths = [math.hypot(*axis) for axis in axes]
    if any(not math.isfinite(value) or value <= 0 for value in lengths):
        raise ValueError('实体的三个长度必须为有限正数')
    directions = [[value/length for value in axis] for axis,length in zip(axes,lengths)]
    if any(abs(dot(a,b)) > 1e-8 for a,b in combinations(directions,2)):
        raise ValueError('实体的边向量必须正交')
    return directions


def make_flat_plate(origin, edges, normal, thickness=None):
    if not _point(origin) or not isinstance(edges,(tuple,list)) or len(edges) != 2:
        raise ValueError('薄板需要有限的三维基点及两条面内边向量')
    axes = [*edges,normal]
    _, _, direction = unit_axes(axes)
    if thickness is not None and (type(thickness) not in (int,float) or not math.isfinite(thickness) or thickness <= 0):
        raise ValueError('薄板厚度必须是有限正数')
    low = [x+min(a,0)+min(b,0) for x,a,b in zip(origin,*edges)]
    span = [abs(a)+abs(b) for a,b in zip(*edges)]
    return {'kind':'flat-plate','origin':low,'dimensions':span,'reference_m':list(origin),
            'edges_m':[list(edge) for edge in edges], 'normal':direction,
            **({'thickness_m':thickness} if thickness is not None else {})}


def box_shape(origin, axes):
    directions = unit_axes(axes)
    indices = [max(range(3),key=lambda j:abs(axis[j])) for axis in directions]
    if len(set(indices)) != 3 or any(abs(axis[j]) < 1-1e-12 or any(abs(x)>1e-9 for i,x in enumerate(axis) if i!=j)
                                    for axis,j in zip(directions,indices)):
        return make_shape('oriented-box',origin,axes)
    low, sizes = list(origin), [0.,0.,0.]
    for edge,j in zip(axes,indices):
        low[j] += min(edge[j],0.)
        sizes[j] = abs(edge[j])
    return {'kind':'box','origin':low,'dimensions':sizes}


def extrude_plate(shape, thickness):
    normal_edge = [value*thickness for value in shape['normal']]
    origin = [x-edge/2 for x,edge in zip(shape['reference_m'],normal_edge)]
    return box_shape(origin,[*shape['edges_m'],normal_edge])


def make_shape(kind, origin, axes):
    if not _point(origin):
        raise ValueError('实体需要有限的三维基点')
    unit_axes(axes)
    if kind == 'cylinder':
        disk = [math.hypot(a,b)/2 for a,b in zip(*axes[:2])]
        low = [a+min(z,0)-r for a,z,r in zip(origin,axes[2],disk)]
        high = [a+max(z,0)+r for a,z,r in zip(origin,axes[2],disk)]
    else:
        points = [transform(origin,axes,point) for point in POLYHEDRA[kind][0]]
        low = [min(values) for values in zip(*points)]
        high = [max(values) for values in zip(*points)]
    size = list(subtract(high,low))
    if not _point(low) or not _point(size) or min(size) <= 0:
        raise ValueError('实体范围退化或超出有限坐标')
    return {'kind':kind,'origin':low,'dimensions':size,'reference_m':list(origin),
            'edges_m':[list(axis) for axis in axes]}


def _box_grid(parts):
    axes = [sorted({point[i] for part in parts for point in part}) for i in range(3)]
    if math.prod(len(axis)-1 for axis in axes) > 2_000_000:
        raise ValueError('组合实体的局部几何超过可处理的 2,000,000 个盒分区')
    index = [{value:i for i,value in enumerate(axis)} for axis in axes]
    filled = set()
    for low, high in parts:
        filled.update(product(*(range(points[a],points[b]) for points,a,b in zip(index,low,high))))
    return axes, filled


def make_box_union(origin, axes, parts):
    if not _point(origin) or not isinstance(parts,list) or not 1 <= len(parts) <= 10000:
        raise ValueError('组合实体需要有限基点和 1–10000 个局部盒')
    directions = unit_axes(axes)
    if any(not math.isclose(math.hypot(*axis),1,abs_tol=1e-9) for axis in axes):
        raise ValueError('组合实体需要单位主轴')
    if any(not isinstance(part,(list,tuple)) or len(part)!=2 or not all(map(_point,part))
           or any(a>=b for a,b in zip(*part)) for part in parts):
        raise ValueError('组合实体的局部盒需要正有限的三个尺寸')
    # Merge round-off equivalents before building a conformal box partition.
    levels = []
    for i in range(3):
        raw = sorted({point[i] for part in parts for point in part})
        tolerance = (raw[-1]-raw[0])*1e-12
        scale, previous = {}, raw[0]
        for coordinate in raw:
            if coordinate-previous > tolerance:
                previous = coordinate
            scale[coordinate] = previous
        levels.append(scale)
    parts = [[[scale[x] for scale,x in zip(levels,point)] for point in part] for part in parts]
    if any(a>=b for part in parts for a,b in zip(*part)):
        raise ValueError('组合实体存在数值上退化的局部盒')
    grid, filled = _box_grid(parts)
    lengths = [[b-a for a,b in zip(axis,axis[1:])] for axis in grid]
    measure = math.fsum(math.prod(values[i] for values,i in zip(lengths,cell)) for cell in filled)
    bounds = [[],[]]
    for low,high in parts:
        vertices = [transform(origin,directions,point) for point in product(*zip(low,high))]
        bounds[0].append([min(values) for values in zip(*vertices)])
        bounds[1].append([max(values) for values in zip(*vertices)])
    low = [min(values) for values in zip(*bounds[0])]
    high = [max(values) for values in zip(*bounds[1])]
    dimensions = list(subtract(high,low))
    if not _point(low) or not _point(dimensions) or min(dimensions)<=0 or not 0<measure<math.inf:
        raise ValueError('组合实体的全局范围或体积无效')
    feature = min(b-a for lower,upper in parts for a,b in zip(lower,upper))
    return {'kind':BOX_UNION, 'origin':low, 'dimensions':dimensions, 'reference_m':list(origin),
            'basis':directions, 'parts':parts, 'volume_m3':measure, 'min_feature_m':feature}


def union_parts(shape):
    origin, basis = shape['reference_m'], shape['basis']
    return [box_shape(transform(origin,basis,low),
                      [[length*v for v in axis] for length,axis in zip(subtract(high,low),basis)])
            for low,high in shape['parts']]


def cartesian_parts(shape):
    basis = shape['basis']
    if any(sum(abs(v)>1e-12 for v in axis)!=1 for axis in basis):
        return None
    origin = shape['reference_m']
    boxes = []
    for low,high in shape['parts']:
        first,second = transform(origin,basis,low),transform(origin,basis,high)
        boxes.append([[min(a,b) for a,b in zip(first,second)],[max(a,b) for a,b in zip(first,second)]])
    return boxes


def _union_relation(shape, point):
    local = [dot(subtract(point,shape['reference_m']),axis) for axis in shape['basis']]
    epsilon = max(shape['dimensions'])*1e-12
    covered = set()
    for low,high in shape['parts']:
        if any(x<a-epsilon or x>b+epsilon for x,a,b in zip(local,low,high)):
            continue
        choices = [tuple(sign for sign,margin in ((-1,x-a),(1,b-x)) if margin>epsilon)
                   for x,a,b in zip(local,low,high)]
        covered.update(product(*choices))
        if len(covered) == 8:
            return 1
    return 0 if covered else -1


def _union_surface(shape):
    axes, filled = _box_grid(shape['parts'])
    origin, basis = shape['reference_m'], shape['basis']
    reflected = dot(cross(*basis[:2]),basis[2]) < 0
    vertices, triangles = {}, []
    for cell in sorted(filled):
        for axis in range(3):
            a,b = (axis+1)%3,(axis+2)%3
            for side in (-1,1):
                neighbor = list(cell)
                neighbor[axis] += side
                if tuple(neighbor) in filled:
                    continue
                face = []
                for da,db in ((0,0),(1,0),(1,1),(0,1)):
                    corner = list(cell)
                    corner[axis] += side==1
                    corner[a] += da
                    corner[b] += db
                    key = tuple(corner)
                    if key not in vertices:
                        vertices[key] = transform(origin,basis,[values[i] for values,i in zip(axes,key)])
                    face.append(vertices[key])
                if (side<0) != reflected:
                    face.reverse()
                triangles.extend(((face[0],face[1],face[2]),(face[0],face[2],face[3])))
    low,high = shape['parts'][0]
    seed = transform(origin,basis,[a+fraction*(b-a) for a,b,fraction in zip(low,high,(.381966,.414214,.618034))])
    return list(vertices.values()), triangles, seed


def validate_shape(shape):
    if not isinstance(shape,dict) or not isinstance(shape.get('kind'),str):
        return
    if shape['kind'] == 'flat-plate':
        canonical = make_flat_plate(shape.get('reference_m'),shape.get('edges_m'),shape.get('normal'),shape.get('thickness_m'))
        if not math.isclose(math.hypot(*shape['normal']),1,rel_tol=1e-9):
            raise ValueError('薄板法向必须为单位向量')
    elif shape['kind'] in SWEPT_KINDS:
        canonical = make_shape(shape['kind'],shape.get('reference_m'),shape.get('edges_m'))
    elif shape['kind'] == BOX_UNION:
        canonical = make_box_union(shape.get('reference_m'),shape.get('basis'),shape.get('parts'))
        for key in ('volume_m3','min_feature_m'):
            value = shape.get(key)
            if type(value) not in (int,float) or not math.isclose(value,canonical[key],rel_tol=1e-9):
                raise ValueError('组合实体体积或最小尺寸与局部几何不一致')
    else:
        return
    for key in ('origin','dimensions'):
        if not _point(shape.get(key)) or any(not math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-12)
                                             for a,b in zip(shape[key],canonical[key])):
            raise ValueError('IR 实体包围盒与原始边向量不一致')


def _basis(shape):
    if shape['kind'] != 'box':
        return shape['reference_m'], shape['edges_m']
    sizes = shape['dimensions']
    return shape['origin'], [[length if axis==i else 0 for axis in range(3)] for i,length in enumerate(sizes)]


def volume(shape):
    if shape['kind'] == BOX_UNION:
        return shape['volume_m3']
    if shape['kind'] == 'flat-plate':
        return math.prod(math.hypot(*edge) for edge in shape['edges_m'])*shape.get('thickness_m',0)
    _, axes = _basis(shape)
    factor = {'cylinder':math.pi/4,'prism':.5,'tet':1/6,'invertedTet':1/6}.get(shape['kind'],1)
    return factor*math.prod(math.hypot(*axis) for axis in axes)


def point_relation(shape, point):
    """Return 1 inside, 0 on the surface, or -1 outside."""
    kind = shape['kind']
    if kind == BOX_UNION:
        return _union_relation(shape,point)
    if kind == 'flat-plate':
        return point_relation(extrude_plate(shape,shape['thickness_m']),point) if 'thickness_m' in shape else -1
    if kind == 'box':
        # Preserve the exact closed/open boundary distinction used by existing IR.
        lo = shape['origin']
        distances = [min(x-a,a+size-x) for x,a,size in zip(point,lo,shape['dimensions'])]
        return (min(distances)>0)-(min(distances)<0)
    origin, axes = _basis(shape)
    delta = subtract(point,origin)
    lengths = [math.hypot(*axis) for axis in axes]
    x,y,z = (dot(delta,[value/length for value in axis])/length for axis,length in zip(axes,lengths))
    if kind == 'cylinder':
        gap = min(.5-math.hypot(x,y),z,1-z)
    elif kind == 'prism':
        gap = min(x,y,1-x-y,z,1-z)
    elif kind in ('tet','invertedTet'):
        if kind == 'invertedTet':
            x,y,z = 1-x,1-y,1-z
        gap = min(x,y,z,1-x-y-z)
    else:
        gap = min(x,y,z,1-x,1-y,1-z)
    return (gap>1e-10)-(gap < -1e-10)


def cylinder_matches(first, second):
    # Endpoint direction and relative orientation of the two radial axes may differ.
    origin, axes = _basis(first)
    other, other_axes = _basis(second)
    end = [a+b for a,b in zip(origin,axes[2])]
    other_end = [a+b for a,b in zip(other,other_axes[2])]
    close = lambda a,b: all(math.isclose(x,y,abs_tol=1e-8,rel_tol=1e-8) for x,y in zip(a,b))
    if not ((close(origin,other) and close(end,other_end)) or (close(origin,other_end) and close(end,other))):
        return False
    rows, other_rows = tuple(zip(*axes[:2])), tuple(zip(*other_axes[:2]))
    scale = max(dot(row,row) for row in (*rows,*other_rows))
    return all(abs(dot(rows[i],rows[j])-dot(other_rows[i],other_rows[j])) <= scale*1e-8
               for i in range(3) for j in range(i,3))


def plates_match(first, second):
    corners = lambda shape: [transform(shape['reference_m'],[*shape['edges_m'],shape['normal']],(i,j,0))
                             for i in (0,1) for j in (0,1)]
    other = corners(second)
    return all(any(math.dist(a,b)<=1e-8 for b in other) for a in corners(first))


def surface(shape):
    kind = shape['kind']
    if kind == BOX_UNION:
        return _union_surface(shape)
    origin, axes = _basis(shape)
    if kind == 'cylinder':
        ring = [(math.cos(math.tau*i/SEGMENTS)/2,math.sin(math.tau*i/SEGMENTS)/2) for i in range(SEGMENTS)]
        coordinates = [(*point,z) for z in (0,1) for point in ring]
        polygons = [tuple(range(SEGMENTS)),tuple(range(SEGMENTS,2*SEGMENTS))]
        for i in range(SEGMENTS):
            j = (i+1)%SEGMENTS
            polygons.append((i,j,j+SEGMENTS,i+SEGMENTS))
    else:
        coordinates, polygons = POLYHEDRA['oriented-box' if kind == 'box' else kind]
    vertices = [transform(origin,axes,point) for point in coordinates]
    # Convex solids admit a strictly interior, deterministic seed away from grid planes.
    centre = [math.fsum(values)/len(vertices) for values in zip(*vertices)]
    seed = [a+(b-a)*.0618033988749895 for a,b in zip(centre,vertices[0])]
    triangles = []
    for polygon in polygons:
        for k in range(1,len(polygon)-1):
            first, second, third = vertices[polygon[0]],vertices[polygon[k]],vertices[polygon[k+1]]
            normal = cross(subtract(second,first),subtract(third,first))
            if dot(normal,subtract(first,centre)) < 0:
                second, third = third, second
            triangles.append((first,second,third))
    return vertices, triangles, seed


def separated(first, second, tolerance):
    """Find a separating plane, projecting curved cylinders analytically."""
    if first['kind'] == BOX_UNION:
        return all(separated(part,second,tolerance) for part in union_parts(first))
    if second['kind'] == BOX_UNION:
        return all(separated(first,part,tolerance) for part in union_parts(second))
    def directions(vectors):
        result = set()
        for vec in vectors:
            length = math.hypot(*vec)
            if length <= 1e-15:
                continue
            axis = tuple(round(v/length,12) for v in vec)
            result.add(axis if next((v for v in axis if v),0) > 0 else tuple(-v for v in axis))
        return sorted(result)

    left, left_faces, _ = surface(first)
    right, right_faces, _ = surface(second)
    def plane_normals(faces):
        return [cross(subtract(b,a),subtract(c,a)) for a,b,c in faces]

    def edge_directions(faces):
        return directions(subtract(b,a) for face in faces for a,b in zip(face,(*face[1:],face[0])))

    origin = first['origin']
    def extent(shape, vertices, direction):
        if shape['kind'] == 'cylinder':
            base, edges = _basis(shape)
            centre = dot(subtract(base,origin),direction)
            height = dot(edges[2],direction)
            radius = math.hypot(*(dot(edge,direction) for edge in edges[:2]))/2
            return centre+min(height,0)-radius,centre+max(height,0)+radius
        projections = [dot(subtract(point,origin),direction) for point in vertices]
        return min(projections),max(projections)

    left_edges, right_edges = edge_directions(left_faces), edge_directions(right_faces)
    for axes in (directions(plane_normals(left_faces)+plane_normals(right_faces)),
                 (cross(a,b) for a in left_edges for b in right_edges)):
        for direction in axes:
            length = math.hypot(*direction)
            if length < 1e-10:
                continue
            a,b = extent(first,left,direction)
            c,d = extent(second,right,direction)
            if max(c-b,a-d) > tolerance*length:
                return True
    return False
