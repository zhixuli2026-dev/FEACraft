"""Flotherm settings.json 白名单、单位换算与 IR 覆盖。"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from .flotherm_flow_devices import apply_device_settings
from .flotherm_ir import MAPPINGS, validate_ir
from .flotherm_parts import enclosure
from .flotherm_refinement import refinement_level, region_levels

_ALLOWED = {"domain", "materials", "objectOverrides", "power", "refinement",
            "parallelSubdomains", "fan", "resistances", "startFrom",
            "mergeTouchingBoxes", "maxSkewness", "scotch", "writeFormat", "boundaries", "openfoam"}
_UNITS = {"w": 1.0, "mw": 1e-3, "kw": 1e3}


@dataclass(frozen=True)
class CHTOptions:
    mode: str
    inlet_velocity: float | None
    base_cell_size: float | None
    end_time: int
    write_interval: int
    monitor_flow: bool
    parallel_subdomains: int
    turbulence: str
    gravity: tuple[float, float, float]


def mesh_skewness(settings):
    if 'maxSkewness' not in settings:
        return None
    value = settings['maxSkewness']
    if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
        raise ValueError('maxSkewness 必须为正有限数')
    return value


def conversion_modes(settings):
    options = settings.get('openfoam', {})
    if not isinstance(options,dict):
        raise ValueError('openfoam 配置必须是对象')
    start_values = [row['startFrom'] for row in (settings,options) if 'startFrom' in row]
    starts = []
    for value in start_values:
        if value not in ('startTime','latestTime','latest'):
            raise ValueError('startFrom 需要 startTime 或 latestTime')
        starts.append('latestTime' if value=='latest' else value)
    merges = [row['mergeTouchingBoxes'] for row in (settings,options) if 'mergeTouchingBoxes' in row]
    if any(type(value) is not bool for value in merges):
        raise ValueError('mergeTouchingBoxes 需要布尔值')
    if len(set(starts))>1 or len(set(merges))>1:
        raise ValueError('重复声明的转换模式设置必须一致')
    return starts[0] if starts else 'startTime', merges[0] if merges else False


def parallel_subdomains(settings):
    counts = [row['parallelSubdomains'] for row in (settings,settings.get('openfoam',{}))
              if 'parallelSubdomains' in row]
    if any(type(count) is not int or not 1<=count<=1024 for count in counts):
        raise ValueError('parallelSubdomains 需要 1–1024 的整数')
    if len(set(counts))>1:
        raise ValueError('重复声明的 parallelSubdomains 必须一致')
    return counts[0] if counts else 2


def cht_options(settings):
    boundaries = settings.get('boundaries')
    if (not isinstance(boundaries, dict) or set(boundaries) - {'mode', 'inletVelocity'}
            or boundaries.get('mode') not in ('channel', 'open')):
        raise ValueError('共轭传热设置需要 boundaries.mode 为 channel 或 open')
    mode, velocity = boundaries['mode'], boundaries.get('inletVelocity')
    options = settings.get('openfoam', {})
    supported = {'baseCellSize', 'endTime', 'writeInterval', 'monitorChannelFlow', 'turbulence', 'gravity',
                 'surfaceRefinementLevel','thinPlateThickness','startFrom','mergeTouchingBoxes','parallelSubdomains'}
    if not isinstance(options, dict) or set(options) - supported:
        raise ValueError('openfoam 共轭传热设置含未支持的字段')
    positive = {key:options[key] for key in ('baseCellSize','thinPlateThickness') if key in options}
    if mode == 'channel' or 'inletVelocity' in boundaries:
        positive['inletVelocity'] = velocity
    for key, value in positive.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} 需要正有限值')
    end = options.get('endTime', 600)
    interval = options.get('writeInterval', end)
    if (type(end) is not int or type(interval) is not int or not 1 <= interval <= end
            or end % interval):
        raise ValueError('endTime 和 writeInterval 需要正整数，且最终步必须写出')
    turbulence = options.get('turbulence', 'laminar')
    if turbulence not in ('laminar', 'RAS', 'realizableKE'):
        raise ValueError('openfoam.turbulence 需要 laminar 或 RAS（realizableKE）')
    gravity = options.get('gravity', [0,-9.81,0] if mode == 'open' else [0,0,0])
    if (not isinstance(gravity, list) or len(gravity) != 3 or
            any(type(value) not in (int, float) or not math.isfinite(value) for value in gravity)):
        raise ValueError('openfoam.gravity 需要三个有限数值')
    monitor = options.get('monitorChannelFlow', mode == 'channel')
    if type(monitor) is not bool:
        raise ValueError('monitorChannelFlow 需要布尔值')
    if monitor and mode == 'open':
        raise ValueError('开放边界没有固定入口和出口，不能启用 monitorChannelFlow')
    if type(settings.get('scotch', True)) is not bool:
        raise ValueError('scotch 需要布尔值')
    refinement = options.get('surfaceRefinementLevel',[1,2])
    if (not isinstance(refinement,list) or len(refinement) != 2 or
            any(type(level) is not int for level in refinement) or not 0 <= refinement[0] <= refinement[1] <= 6):
        raise ValueError('surfaceRefinementLevel 需要 0–6 之间有序的两个整数')
    count = parallel_subdomains(settings)
    return CHTOptions(mode, velocity if mode == 'channel' else None, options.get('baseCellSize'), end,
                      interval, monitor, count, 'laminar' if turbulence == 'laminar' else 'realizableKE',
                      tuple(gravity))


def _power(value) -> float:
    if isinstance(value, dict):
        unit = value.get("unit", "W")
        if not isinstance(unit, str) or unit.lower() not in _UNITS:
            raise ValueError(f"未知功率单位: {unit}")
        value = value.get("value")
        return _power(value) * _UNITS[unit.lower()]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("功率必须是 >=0 的有限值或 {value,unit}")
    return float(value)


def apply_settings(ir: dict, settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("settings.json 必须是对象")
    unknown = set(settings) - _ALLOWED
    if unknown:
        raise ValueError(f"未知 settings 项: {sorted(unknown)}")
    result = copy.deepcopy(ir)
    objects = {obj["id"]: obj for obj in result["objects"]}
    for key in ("objectOverrides", "power"):
        values = settings.get(key, {})
        if not isinstance(values, dict):
            raise ValueError(f"{key} 必须是对象")
        missing = set(values) - set(objects)
        if missing:
            raise ValueError(f"{key} 引用未知对象: {sorted(missing)}")
    for oid, override in settings.get("objectOverrides", {}).items():
        if not isinstance(override, dict):
            raise ValueError("objectOverrides 每项必须是对象")
        unknown = set(override) - {"material", "mapping", "power_w", "refinement", "plateThickness", "wallThickness"}
        if unknown:
            raise ValueError(f"{oid} 未知覆盖项: {sorted(unknown)}")
        if "material" in override:
            if override["material"] not in result["materials"] and override["material"] not in settings.get("materials", {}):
                raise ValueError(f"{oid} 覆盖引用未知材料")
            objects[oid]["material"] = override["material"]
        if "mapping" in override:
            if override["mapping"] not in MAPPINGS:
                raise ValueError("覆盖 mapping 无效")
            objects[oid]["mapping"] = override["mapping"]
        if "power_w" in override:
            objects[oid]["power_w"] = _power(override["power_w"])
        if 'plateThickness' in override:
            thickness = override['plateThickness']
            if (objects[oid].get('shape',{}).get('kind') != 'flat-plate' or type(thickness) not in (int,float)
                    or not math.isfinite(thickness) or thickness <= 0):
                raise ValueError(f'{oid} 的 plateThickness 仅用于薄板，且需要正有限米数')
            objects[oid]['plate_thickness_override_m'] = thickness
        if 'wallThickness' in override:
            obj = objects[oid]
            shape = obj['shape']
            if obj.get('type')!='enclosure' or shape.get('kind') not in ('box','box-union'):
                raise ValueError(f'{oid} 的 wallThickness 仅用于轴对齐外壳')
            original = obj.get('enclosure',{})
            obj['shape'], shell = enclosure(shape['origin'],shape['dimensions'],override['wallThickness'])
            obj['enclosure'] = {**shell,'thickness_source':'objectOverrides.wallThickness',
                                'source_wall_thickness_m':original.get('source_wall_thickness_m',original.get('wall_thickness_m'))}
            obj['mapping'] = 'equivalent'
            obj.pop('reason',None)
            obj.pop('missing_fields',None)
        if "refinement" in override:
            objects[oid]["refinement"] = refinement_level(override["refinement"])
    for oid, value in settings.get("power", {}).items():
        objects[oid]["power_w"] = _power(value)
    apply_device_settings(objects, settings)
    result["materials"].update(settings.get("materials", {}))
    mesh_skewness(settings)
    if "domain" in settings:
        bbox = settings["domain"]
        if not isinstance(bbox, list) or len(bbox) != 6 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in bbox):
            raise ValueError("domain 必须为六数值 bbox")
        if bbox[0] >= bbox[3] or bbox[1] >= bbox[4] or bbox[2] >= bbox[5]:
            raise ValueError("domain bbox 最小值必须小于最大值")
    coverage = {key: 0 for key in MAPPINGS}
    for obj in result["objects"]:
        coverage[obj["mapping"]] += 1
    result["coverage"] = {"objects": len(result["objects"]), **coverage,
                          "power_w": sum(obj.get("power_w", 0) for obj in result["objects"])}
    result['settings'] = {**result.get('settings', {}), **{
        key: settings[key] for key in settings if key != 'objectOverrides'}}
    region_levels(result['settings'],result.get('regions',()))
    conversion_modes(result['settings'])
    parallel_subdomains(result['settings'])
    if {'boundaries', 'openfoam'} & result['settings'].keys():
        cht_options(result['settings'])
    return validate_ir(result)


def load_settings(ir: dict, path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("settings.json 不能超过 1 MiB")
    result = apply_settings(ir, json.loads(raw))
    result["settings"]["source"] = {"path": path.name, "sha256": hashlib.sha256(raw).hexdigest()}
    return result
