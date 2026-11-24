"""Map compact two-resistance models to case and PCB regions."""
import math
from pathlib import Path


MATERIAL_ID = '_two_resistance_case_equivalent'
MATERIAL = {'conductivity_w_mk':10.,'density_kg_m3':1000.,'specific_heat_j_kg_k':1000.,'rho_cp':1e6}
NATIVE_FILES = ('twoResistanceNetworkSource.C','twoResistanceNetworkSource.H','Make/files','Make/options')
LIBRARY = 'libvibeflowFlothermTwoR.so'
SOURCE_DIR = 'constant/flotherm-two-r-src'
LIBRARY_DIR = 'constant/flotherm-libs'


def parameters(case_resistance, board_resistance, normal):
    resistances = (case_resistance,board_resistance)
    if (any(type(r) not in (int,float) or not math.isfinite(r) or r<=0 or not math.isfinite(1/r) for r in resistances)
            or not math.isfinite(sum(resistances))):
        raise ValueError('2R 的 thetaJC 和 thetaJB 必须是正有限的 K/W 热阻')
    if (not isinstance(normal,(list,tuple)) or len(normal)!=3 or
            any(type(value) not in (int,float) or not math.isfinite(value) for value in normal) or
            not math.isclose(math.hypot(*normal),1,rel_tol=1e-9)):
        raise ValueError('2R 需要明确的单位法向')
    return {'theta_jc_k_w':case_resistance,'theta_jb_k_w':board_resistance,'normal':list(normal)}


def validate_object(obj):
    if obj.get('type') != 'twoResistorModel':
        return
    data = obj.get('two_resistance')
    if not isinstance(data,dict) or obj.get('shape',{}).get('kind') not in ('box','flat-plate'):
        raise ValueError('2R 需要已确认的热阻和轴对齐几何')
    parameters(data.get('theta_jc_k_w'),data.get('theta_jb_k_w'),data.get('normal'))


def axis_and_sign(normal):
    axis = max(range(3),key=lambda i:abs(normal[i]))
    return (axis,1 if normal[axis]>0 else -1) if abs(normal[axis])>=1-1e-9 else None


def case_cap(shape, normal, epsilon):
    axis, sign = axis_and_sign(normal)
    low, sizes = list(shape['origin']),list(shape['dimensions'])
    thickness = min(max(.45*sizes[axis],epsilon),.8*sizes[axis])
    if sign > 0:
        low[axis] += sizes[axis]-thickness
    sizes[axis] = thickness
    return {'kind':'box','origin':low,'dimensions':sizes}


def _choose_board(device, boards):
    axis, sign = axis_and_sign(device['two_resistance']['normal'])
    lower, upper = device['device_bounds_m']
    board_face = lower[axis] if sign>0 else upper[axis]
    tangents = [i for i in range(3) if i!=axis]
    candidates = []
    for region, board in boards:
        basis = axis_and_sign(board['pcb']['normal'])
        if basis is None or basis[0] != axis or board['shape']['kind'] != 'box':
            continue
        lo, hi = board['bounds_m']
        surface = hi[axis] if sign>0 else lo[axis]
        gap = sign*(board_face-surface)
        overlap = [min(upper[i],hi[i])-max(lower[i],lo[i]) for i in tangents]
        if gap < -1e-9 or min(overlap)<=0:
            continue
        candidates.append(((max(gap,0),-math.prod(overlap)),region,board))
    values = sorted(candidates,key=lambda row:row[0])
    if not values:
        return None,'2R 沿板侧法向没有找到投影重叠且平行的 PCB'
    if len(values)>1 and all(math.isclose(a,b,abs_tol=1e-12,rel_tol=1e-8) for a,b in zip(values[0][0],values[1][0])):
        return None,'2R 投影对应多个等距且重叠面积相同的 PCB'
    (gap, negative_overlap),region,board = values[0]
    return (region, board, gap,-negative_overlap),None


def networks(solids):
    boards = [(region,row) for region,row in solids.items() if 'pcb' in row]
    compiled, blockers = [], []
    for case_region, device in solids.items():
        if 'two_resistance' not in device:
            continue
        selected, error = _choose_board(device,boards)
        if error:
            blockers.append({'object':device['id'],'reason':error})
            continue
        board_region, board, gap, overlap = selected
        compiled.append({'id':device['id'], 'case_region':case_region, 'board_region':board_region,
                         'board_object':board['id'],'power_w':device['power_w'],
                         'case_bounds_m':device['bounds_m'],'board_bounds_m':board['bounds_m'],
                         'board_gap_m':gap,'projected_overlap_m2':overlap,
                         'case_temperature_function':f'twoR_{case_region}_caseT',
                         'board_temperature_function':f'twoR_{case_region}_boardT',
                         **device['two_resistance']})
    return compiled,blockers


def _bounds(values):
    return '('+' '.join('('+' '.join(map(str,point))+')' for point in values)+')'


def source_entries(items, region):
    entries = []
    for item in items:
        case_side = region == item['case_region']
        if not case_side and region != item['board_region']:
            continue
        local, remote = ('case','board') if case_side else ('board','case')
        rc, rb = item['theta_jc_k_w'],item['theta_jb_k_w']
        entries.append(f'''twoR_{item['case_region']}_{local}
{{
    type twoResistanceNetworkSource;
    active true;
    fields (h);
    deviceId {item['id']};
    localRegion {region};
    remoteRegion {item[remote+'_region']};
    localBounds {_bounds(item[local+'_bounds_m'])};
    remoteBounds {_bounds(item[remote+'_bounds_m'])};
    power {item['power_w']};
    localResistance {rc if case_side else rb};
    remoteResistance {rb if case_side else rc};
}}''')
    return entries


def write_native_sources(output):
    source = Path(__file__).with_name('flotherm_two_r_native')
    for name in NATIVE_FILES:
        destination = output/SOURCE_DIR/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((source/name).read_bytes())
