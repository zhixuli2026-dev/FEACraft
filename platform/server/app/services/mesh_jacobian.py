"""Minimum scaled Jacobian of native linear volume cells in an OpenFOAM VTK export."""
from array import array
import math
import re
import xml.etree.ElementTree as ET


METHOD = 'verdict-scaled-jacobian'
VTK_DIRECTORY = 'mesh-quality-vtk'
VTK_EVIDENCE = 'mesh-quality.vtu'
CELL_COUNTS = re.compile(r'^\s*cells:\s*(\d+)\s*$', re.M)
_HEX_CORNERS = ((1, 3, 4), (2, 0, 5), (3, 1, 6), (0, 2, 7),
                (7, 5, 0), (4, 6, 1), (5, 7, 2), (6, 4, 3))
_TET_CORNERS = ((1, 2, 3), (2, 0, 3), (0, 1, 3), (2, 1, 0))
_WEDGE_CORNERS = ((3, 1, 2), (4, 2, 0), (5, 0, 1), (4, 0, 5), (5, 1, 3), (4, 2, 3))
_CELL_SIZES = {10: 4, 12: 8, 13: 6, 14: 5}


def _subtract(a, b):
    return tuple(x-y for x,y in zip(a,b))


def _scaled(a, b, c):
    vectors=[]
    for vector in (a,b,c):
        length=math.hypot(*vector)
        if not length:
            return 0.0
        vectors.append(tuple(value/length for value in vector))
    (ax,ay,az),(bx,by,bz),(cx,cy,cz)=vectors
    return ax*(by*cz-bz*cy)+ay*(bz*cx-bx*cz)+az*(bx*cy-by*cx)


def _corners(points, corners):
    return [_scaled(*(_subtract(points[index], points[origin]) for index in triplet))
            for origin,triplet in enumerate(corners)]


def _tetrahedron(points):
    values=_corners(points,_TET_CORNERS)
    # For a linear tet the signed determinant is constant. The largest
    # product of incident edge lengths supplies Verdict's denominator.
    return math.sqrt(2)*min(values,key=abs)


def _hexahedron(points):
    # The three trilinear derivatives at the center are the corresponding
    # parallel edge sums. The common factor of one quarter cancels.
    groups=(((1,0),(2,3),(5,4),(6,7)), ((3,0),(2,1),(7,4),(6,5)),
            ((4,0),(5,1),(6,2),(7,3)))
    edges=[tuple(sum(components) for components in zip(*(_subtract(points[a],points[b]) for a,b in group)))
           for group in groups]
    return min(_scaled(*edges),*_corners(points,_HEX_CORNERS))


def _pyramid(points):
    center=tuple(sum(coordinates)/4 for coordinates in zip(*points[:4]))
    minimum=min(_tetrahedron((points[index],points[(index+1)%4],center,points[4])) for index in range(4))
    return max(0.0,1-abs(1-math.sqrt(2)*minimum))


_MEASURE = {10:_tetrahedron, 12:_hexahedron,
            13:lambda points: 2/math.sqrt(3)*min(_corners(points,_WEDGE_CORNERS)), 14:_pyramid}


def _values(node, kind, parse):
    if node is None or node.get('format')!='ascii':
        raise ValueError('Expected native ASCII VTK mesh arrays')
    # An array avoids storing every token as a separate Python object.
    return array(kind,(parse(match.group()) for match in re.finditer(r'\S+',node.text or '')))


def measure(stream, expected_cells):
    """Require the complete native cell count. Arbitrary polyhedra lack a reference-cell Jacobian."""
    root=ET.parse(stream).getroot()
    pieces=root.findall('./UnstructuredGrid/Piece')
    if root.tag!='VTKFile' or len(pieces)!=1 or not expected_cells:
        raise ValueError('Expected one native volume mesh in the VTK export')
    piece=pieces[0]
    if int(piece.get('NumberOfCells','0'))!=expected_cells:
        raise ValueError('VTK cell count differs from checkMesh')
    points=_values(piece.find('./Points/DataArray'),'d',float)
    cells=piece.find('./Cells')
    if cells is None or len(points)!=3*int(piece.get('NumberOfPoints','0')):
        raise ValueError('Incomplete native VTK geometry')
    ids=_values(cells.find("./DataArray[@Name='connectivity']"),'q',int)
    offsets=_values(cells.find("./DataArray[@Name='offsets']"),'q',int)
    kinds=_values(cells.find("./DataArray[@Name='types']"),'B',int)
    if len(offsets)!=expected_cells or len(kinds)!=expected_cells or offsets[-1]!=len(ids):
        raise ValueError('Incomplete native VTK topology')
    start=0
    minimum=math.inf
    for end,kind in zip(offsets,kinds):
        if kind not in _MEASURE:
            raise ValueError(f'Scaled Jacobian is unavailable for VTK cell type {kind}')
        if end-start!=_CELL_SIZES[kind] or end>len(ids):
            raise ValueError('Invalid native VTK cell size')
        indexes=ids[start:end]
        if any(index<0 or index>=len(points)//3 for index in indexes):
            raise ValueError('Invalid native VTK point index')
        value=_MEASURE[kind]([points[3*index:3*index+3] for index in indexes])
        if not math.isfinite(value):
            raise ValueError('Non-finite scaled Jacobian')
        minimum=min(minimum,value)
        start=end
    return minimum
