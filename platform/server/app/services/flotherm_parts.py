"""Build geometric parts whose source records describe a single physical solid."""
from itertools import product
import math

from .flotherm_geometry import BOX_UNION, IDENTITY, make_box_union, transform


def enclosure(origin, dimensions, thickness):
    if (type(thickness) not in (int,float) or not math.isfinite(thickness) or thickness<=0
            or any(2*thickness>=size for size in dimensions)):
        raise ValueError('外壳壁厚必须是有限正数，且两倍壁厚小于每个外尺寸')
    inner_low, inner_high = [thickness]*3, [size-thickness for size in dimensions]
    parts = []
    for axis in range(3):
        for side in (0,1):
            low = [inner_low[i] if i<axis else 0.0 for i in range(3)]
            high = [inner_high[i] if i<axis else dimensions[i] for i in range(3)]
            if side:
                low[axis] = inner_high[axis]
            else:
                high[axis] = inner_low[axis]
            parts.append([low,high])
    shape = make_box_union(origin,IDENTITY,parts)
    cavity = [[x+a for x,a in zip(origin,point)] for point in (inner_low,inner_high)]
    return shape, {'wall_thickness_m':thickness,'cavity_bounds_m':cavity}


def validate_enclosure(obj):
    if obj.get('type') != 'enclosure' or obj.get('mapping') not in ('converted','equivalent'):
        return
    shape, details = obj.get('shape',{}), obj.get('enclosure',{})
    if shape.get('kind') != BOX_UNION or not isinstance(details,dict):
        raise ValueError('已映射的外壳需要六面壁及空腔坐标')
    canonical, geometry = enclosure(shape['origin'],shape['dimensions'],details.get('wall_thickness_m'))
    cavity = details.get('cavity_bounds_m')
    if (not unions_match(shape,canonical) or not isinstance(cavity,list) or len(cavity)!=2
            or any(not isinstance(point,list) or len(point)!=3
                   or any(type(x) not in (int,float) or not math.isclose(x,y,rel_tol=1e-9,abs_tol=1e-12)
                          for x,y in zip(point,expected)) for point,expected in zip(cavity,geometry['cavity_bounds_m']))):
        raise ValueError('外壳的六面壁与壁厚或空腔坐标不一致')


def _world_corners(shape):
    origin, basis = shape['reference_m'], shape['basis']
    return [sorted(tuple(transform(origin,basis,point)) for point in product(*zip(low,high)))
            for low,high in shape['parts']]


def unions_match(first, second):
    left, right = _world_corners(first), _world_corners(second)
    if len(left)!=len(right):
        return False
    # Sorting by the actual corners supplies a stable ordering for translated axes and reflected parts.
    return all(math.dist(a,b)<=1e-8 for part,other in zip(sorted(left),sorted(right)) for a,b in zip(part,other))


def _spacing(span,width,count,gap,low=0.,high=0.):
    if not math.isfinite(count) or not 1<=count<=1000 or count!=int(count):
        raise ValueError('散热器鳍片数量必须是 1–1000 之间的整数')
    count = int(count)
    available = span-low-high-width
    if available<0 or min(low,high,gap)<0:
        raise ValueError('散热器鳍片与边距超过底座范围')
    if count==1:
        if gap:
            raise ValueError('单个鳍片无法使用配对中间气隙')
        return [low+available/2]
    if gap and (count%2 or count<4):
        raise ValueError('有中间气隙的排列需要至少四个对称鳍片')
    pitch = (available-gap-width)/(count-2) if gap else available/(count-1)
    if pitch<=width:
        raise ValueError('散热器鳍片之间需要正空气间隙')
    extra = gap+width-pitch
    return [low+i*pitch+(extra if gap and i>=count//2 else 0) for i in range(count)]


def pdml_heatsink(record, origin, axes):
    """Read the V2310 uniform plate-fin and inline rectangular pin-fin modes."""
    def values(offset,count):
        data = record.values(0x2034000+offset)
        if len(data)!=count or any(type(value) not in (int,float) or not math.isfinite(value) for value in data):
            raise ValueError(f'PDML 散热器 {record.name} 缺少完整字段 0x{0x2034000+offset:x}')
        return data

    def one(offset):
        return values(offset,1)[0]

    if (one(1) not in (0x303e001,0x303e003) or values(6,2)!=[0x302a001,0x302c001]
            or any(one(i)!=0x307a001 for i in (0x24,0x25))
            or any(one(i)!=0x306a001 for i in (9,0xe,0x13))
            or any(one(i)!=0x302a001 for i in (0x21,0x22,0x23))):
        raise ValueError('PDML 散热器需要已确认的均匀、挤出、详细几何模式')
    width,length,base = values(2,3)
    height,thickness,_ = values(7,3)
    if min(width,length,base,height,thickness)<=0:
        raise ValueError('PDML 散热器底座和鳍片尺寸必须为正')
    nx,ny = values(0x19,2)
    gx,gy = values(0x20,2)
    mode = one(0x18)
    parts = [[[0.,0.,0.],[length,width,base]]]
    if mode==0x307b001:
        left,right = values(4,2)
        rear,front = values(0x26,2)
        if gx or min(rear,front)<0 or rear+front>=length:
            raise ValueError('PDML 板翅的纵向气隙或内嵌量不受支持')
        ys = _spacing(width,thickness,ny+2,gy,left,right)
        parts.extend([[front,y,base],[length-rear,y+thickness,base+height]] for y in ys)
        family = 'uniformPlateFin'
    elif mode==0x307b002 and one(0x1a)==0x307c001 and one(0x1b)==0x307d001:
        pin_length = one(0x1c)
        if pin_length<=0 or nx*ny>9999:
            raise ValueError('PDML 矩形针翅长度或总数量无效')
        xs,ys = _spacing(length,pin_length,nx,gx),_spacing(width,thickness,ny,gy)
        parts.extend([[x,y,base],[x+pin_length,y+thickness,base+height]] for x,y in product(xs,ys))
        family = 'inlineRectangularPinFin'
    else:
        raise ValueError('PDML 散热器需要均匀板翅或共线矩形针翅')
    shape = make_box_union(origin,axes,parts)
    return shape, {'family':family,'fin_count':len(parts)-1,'base_thickness_m':base}
