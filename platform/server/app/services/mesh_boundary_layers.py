"""Measure layers from the actual wall faces and cells of a checked blockMesh."""

from array import array
import math
import re
from itertools import repeat, zip_longest

from .case_mesh_schema import boundary_layer_plan


def declaration(mesh):
    if 'boundaryLayers' in mesh:
        return boundary_layer_plan(mesh['boundaryLayers'])
    if str(mesh.get('boundaryLayerRequired')).lower()=='true':
        raise ValueError('要求边界层时须提供 mesh.boundaryLayers 的壁面和尺寸参数')


def _mesh_rows(path):
    """Read the counted, uncompressed ASCII lists emitted by blockMesh."""
    with path.open(encoding='ascii') as stream:
        for line in stream:
            if line.lstrip().startswith('FoamFile'):
                break
        else:
            raise ValueError(f'{path.name} 缺少 OpenFOAM 文件头')
        header=[]
        for line in stream:
            if line.strip()=='}':
                break
            header.append(line)
        if not re.search(r'(?m)^\s*format\s+ascii\s*;', ''.join(header)):
            raise ValueError('边界层证据需要 blockMesh 的 ASCII polyMesh')
        rows=(line.partition('//')[0].strip() for line in stream)
        rows=(line for line in rows if line)
        first=next(rows,'')
        compact=re.fullmatch(r'(\d+)\s*(?:\((.*)\)|\{([^{}]+)\})',first)
        if compact:
            count=int(compact[1])
            if compact[3] is not None and path.name in ('owner','neighbour') and compact[3].strip().isdecimal():
                records=repeat(compact[3].strip(),count)
            elif compact[2] is not None:
                body=compact[2]
                if path.name in ('owner','neighbour'):
                    records=body.split()
                else:
                    pattern=r'\([^()]+\)' if path.name=='points' else r'\d+\([^()]+\)'
                    if re.sub(pattern,'',body).strip():
                        raise ValueError(f'{path.name} 含无法识别的记录')
                    records=re.findall(pattern,body)
                if len(records)!=count:
                    raise ValueError(f'{path.name} 的记录与声明数量不符')
            else:
                raise ValueError(f'{path.name} 的 OpenFOAM 紧凑列表格式不受支持')
            if next(rows,None) is not None:
                raise ValueError(f'{path.name} 含额外网格记录')
            yield from records
            return
        if not first.isdecimal() or next(rows,None)!='(':
            raise ValueError(f'{path.name} 的 OpenFOAM 列表格式不受支持')
        for _ in range(int(first)):
            row=next(rows,'')
            if not row or row==')':
                raise ValueError(f'{path.name} 的记录少于声明数量')
            yield row
        if next(rows,None)!=')' or next(rows,None) is not None:
            raise ValueError(f'{path.name} 的记录与声明数量不符')


def _coordinate(row, axis):
    values=row[1:-1].split()
    if not row.startswith('(') or not row.endswith(')') or len(values)!=3:
        raise ValueError('polyMesh 网格点须有三个坐标')
    coordinates=tuple(map(float,values))
    if not all(map(math.isfinite,coordinates)):
        raise ValueError('polyMesh 含非有限网格坐标')
    return coordinates[axis]


def _face_span(row, coordinates):
    match=re.fullmatch(r'(\d+)\(([^()]*)\)',row)
    if not match:
        raise ValueError('polyMesh 网格面格式无效')
    vertices=tuple(map(int,match[2].split()))
    if len(vertices)!=int(match[1]) or len(vertices)<3 or not all(0<=point<len(coordinates) for point in vertices):
        raise ValueError('polyMesh 网格面引用了无效的网格点')
    positions=[coordinates[point] for point in vertices]
    return min(positions),max(positions)


def measure(directory, patches, plan, cell_count):
    patch=next((patch for patch in patches if patch['name']==plan['patch']),None)
    if not patch or patch['type']!='wall' or patch['nFaces']<1:
        raise ValueError('指定边界层网格面必须是实际存在的非空 wall')
    axis='xyz'.index(plan['axis'])
    coordinates=array('d',(_coordinate(row,axis) for row in _mesh_rows(directory/'points')))
    if not coordinates or cell_count<1:
        raise ValueError('边界层网格缺少点或单元')
    domain_min,domain_max=min(coordinates),max(coordinates)
    owners=array('q',map(int,_mesh_rows(directory/'owner')))
    neighbours=array('q',map(int,_mesh_rows(directory/'neighbour')))
    start,end=patch['startFace'],patch['startFace']+patch['nFaces']
    if not len(neighbours)<=start<end<=len(owners):
        raise ValueError('边界层壁面索引超出实际边界网格面范围')

    lows=array('d',[math.inf])*cell_count
    highs=array('d',[-math.inf])*cell_count
    # Planar internal faces connect each measured cell to its next normal layer.
    links=array('q')
    planes=array('d')
    wall=set()
    for face,(row,owner) in enumerate(zip_longest(_mesh_rows(directory/'faces'),owners)):
        if row is None or owner is None or not 0<=owner<cell_count:
            raise ValueError('polyMesh 的网格面与 owner 对应关系无效')
        low,high=_face_span(row,coordinates)
        cells=(owner,)
        if face<len(neighbours):
            other=neighbours[face]
            if not 0<=other<cell_count or other==owner:
                raise ValueError('polyMesh 的内部面邻接单元无效')
            cells=(owner,other)
            if low==high:
                links.extend(cells)
                planes.append(low)
        if start<=face<end:
            if low!=high or low not in (domain_min,domain_max):
                raise ValueError('边界层壁面须位于指定法向轴的外侧平面')
            wall.add(low)
        for cell in cells:
            lows[cell]=min(lows[cell],low)
            highs[cell]=max(highs[cell],high)

    if len(wall)!=1 or domain_min==domain_max:
        raise ValueError('边界层壁面须位于单一平面且具有非零法向跨度')
    positive=next(iter(wall))==domain_min
    toward=array('q',[-1])*cell_count
    for index,plane in enumerate(planes):
        first,second=links[index*2],links[index*2+1]
        if lows[first]==plane and highs[second]==plane:
            first,second=second,first
        if highs[first]!=plane or lows[second]!=plane:
            continue
        if not positive:
            first,second=second,first
        toward[first]=second if toward[first]==-1 else -2

    front=owners[start:end]
    thickness_ranges=[]
    greatest_growth=1.0
    previous=None
    for depth in range(plan['minLayers']):
        if any(cell<0 for cell in front) or len(set(front))!=len(front):
            raise ValueError('壁面各网格面未形成达到指定层数的一一连续单元层')
        thickness=[highs[cell]-lows[cell] for cell in front]
        if any(not math.isfinite(height) or height<=0 for height in thickness):
            raise ValueError('壁面单元层缺少有效的实际法向厚度')
        if previous is not None:
            greatest_growth=max(greatest_growth,max(height/previous[i] for i,height in enumerate(thickness)))
        thickness_ranges.append([min(thickness),max(thickness)])
        if depth+1<plan['minLayers']:
            previous=thickness
            front=array('q',(toward[cell] for cell in front))

    first_height=thickness_ranges[0][1]
    if first_height>plan['maxFirstLayerHeight'] and not math.isclose(first_height,plan['maxFirstLayerHeight'],rel_tol=1e-8):
        raise ValueError('实际首层高度超过 mesh.boundaryLayers.maxFirstLayerHeight')
    if greatest_growth>plan['maxGrowth'] and not math.isclose(greatest_growth,plan['maxGrowth'],rel_tol=1e-8):
        raise ValueError('实际层增长率超过 mesh.boundaryLayers.maxGrowth')
    return {'plan':plan,'wall_faces':patch['nFaces'],'inward_direction':('+' if positive else '-')+plan['axis'],
            'thickness_ranges_m':thickness_ranges,'maximum_growth':greatest_growth}
