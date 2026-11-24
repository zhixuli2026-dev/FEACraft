"""Independently recompute steady 2R branch heat from native OpenFOAM temperatures."""
import json
import math
import re

from .flotherm_channel import _latest, _latest_scalar
from .flotherm_heat_balance import NUMBER, _read_bytes


_WORD = r'[A-Za-z_][A-Za-z0-9_.-]{0,127}'
_ROW = re.compile(rf'twoResistanceNetworkSource device=({_WORD}) time=({NUMBER}) localRegion=({_WORD}) '
                  rf'Tlocal=({NUMBER}) Tremote=({NUMBER}) Tjunction=({NUMBER}) Qlocal=({NUMBER})')


def _native_branches(data, moment):
    branches = {}
    for line in data.decode('utf8').splitlines():
        row = _ROW.fullmatch(line.strip())
        if not row:
            raise ValueError('2R 原生分热日志存在未知或不完整记录')
        iteration = float(row[2])
        values = tuple(map(float,row.groups()[3:]))
        if not math.isfinite(iteration) or not all(map(math.isfinite,values)):
            raise ValueError('2R 原生分热日志包含非有限数值')
        if iteration != float(moment):
            continue
        key = row[1],row[3]
        if key in branches:
            raise ValueError(f'2R 原生分热日志存在重复记录：{row[1]}/{row[3]}')
        branches[key] = values
    return branches


def evaluate(root, relative_tolerance=.02):
    if not math.isfinite(relative_tolerance) or not 0 < relative_tolerance < 1:
        raise ValueError('2R 相对容差必须大于 0 且小于 1')
    digests = {}
    manifest = json.loads(_read_bytes(root,'case-manifest.json',digests))
    items = manifest.get('two_resistance_networks') if isinstance(manifest,dict) else None
    if not isinstance(items,list) or not items or manifest.get('required_overrides'):
        raise ValueError('算例没有可核验的 2R 网络')
    fluid = manifest.get('fluid_region')
    if not isinstance(fluid,str) or not re.fullmatch(_WORD,fluid):
        raise ValueError('2R 清单的流体区域名无效')
    moment = _latest(root,fluid+'/')
    log = 'log.twoResistance'
    measured = _native_branches(_read_bytes(root,log,digests),moment)
    results, reasons, used = [], [], set()
    for item in items:
        try:
            identity = item['id']
            names = [item[branch+'_region'] for branch in ('case','board')]
            functions = [item[branch+'_temperature_function'] for branch in ('case','board')]
            power, rc, rb = (item[key] for key in ('power_w','theta_jc_k_w','theta_jb_k_w'))
            if (not all(isinstance(name,str) and re.fullmatch(_WORD,name) for name in (identity,*names,*functions))
                    or names[0] == names[1] or any(type(value) not in (int,float) or not math.isfinite(value)
                                                   for value in (power,rc,rb))
                    or power < 0 or min(rc,rb) <= 0 or not math.isfinite(rc+rb)):
                raise ValueError('2R 清单的区域、功率或热阻无效')
        except (TypeError, KeyError) as error:
            raise ValueError('2R 清单存在不完整的器件定义') from error
        keys = [(identity,name) for name in names]
        if any(key not in measured or key in used for key in keys):
            raise ValueError(f'器件 {identity} 缺少唯一的最新时刻 2R 原生分热记录')
        used.update(keys)
        case_t, board_t = [_latest_scalar(root,digests,region,function,moment,filename='volFieldValue.dat')
                           for region,function in zip(names,functions)]
        if min(case_t,board_t) <= 0:
            raise ValueError('2R 区域温度必须是正的开尔文温度')
        q_case = (power*(rb/(rc+rb)) + (board_t-case_t)/(rc+rb))
        q_board = power-q_case
        junction = case_t+q_case*rc
        native = [measured[key] for key in keys]
        logged = [row[3] for row in native]
        tolerance = max(1e-5,relative_tolerance*max(power,abs(q_case),abs(q_board)))
        temp_tolerance = max(.002,tolerance*min(rc,rb))
        if not all(math.isfinite(value) for value in (q_case,q_board,junction,tolerance,temp_tolerance)):
            raise ValueError('2R 回算结果不是有限值')
        if abs(sum(logged)-power) > tolerance:
            reasons.append({'code':'branch_power_imbalance','device':identity})
        branches = {}
        for label, region, r, expected, temperatures, observed in zip(
                ('case','board'),names,(rc,rb),(q_case,q_board),((case_t,board_t),(board_t,case_t)),native):
            local, remote, native_junction, actual = observed
            if (abs(actual-expected)>tolerance or abs(native_junction-junction)>temp_tolerance or
                    any(abs(a-b)>temp_tolerance for a,b in zip((local,remote),temperatures))):
                reasons.append({'code':'branch_result_mismatch','device':identity,'branch':label})
            branches[label] = {'region':region,'resistance_k_w':r,'temperature_k':temperatures[0],
                               'native_temperature_k':local,'native_junction_k':native_junction,
                               'recomputed_power_w':expected,'native_power_w':actual}
        results.append({'id':identity,'power_w':power,'junction_k':junction,'tolerance_w':tolerance,
                        'native_power_residual_w':sum(logged)-power,'branches':branches})
    if used != measured.keys():
        raise ValueError('2R 最新时刻日志包含清单以外的器件或区域')
    return {'schema':'vibeflow.flotherm.two-resistance.v1','time':moment,'native_log':log,
            'networks':results,'result_sha256':digests,'ok':not reasons,'reasons':reasons}
