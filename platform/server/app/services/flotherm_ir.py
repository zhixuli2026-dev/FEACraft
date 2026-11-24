"""Flotherm 中间表示（IR）构建、引用校验与确定性读写。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from .flotherm_flow_devices import validate_device
from .flotherm_geometry import unit_axes, validate_shape
from .flotherm_parts import validate_enclosure
from .flotherm_two_resistance import validate_object
import tempfile

SCHEMA = "vibeflow.flotherm.ir.v1"
MAPPINGS = {"converted", "equivalent", "unsupported", "missing-info"}


def _finite(value, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} 必须是 >= {minimum} 的有限数值")
    return float(value)


def _coordinates(values, size):
    return (isinstance(values, list) and len(values) == size
            and all(type(value) in (int, float) and math.isfinite(value) for value in values))


def _observation_name(row):
    return all(isinstance(row.get(key), str) and row[key] for key in ('id', 'name', 'path'))


def _observation_source(value):
    if not isinstance(value, dict) or not isinstance(value.get('file'), str) or not value['file']:
        return False
    if value.get('format') == 'ECXML':
        return isinstance(value.get('element'), str) and bool(value['element'])
    return value.get('format') == 'PDML' and type(value.get('offset')) is int and value['offset'] >= 0


def validate_ir(data: dict):
    if data.get("schema") != SCHEMA:
        raise ValueError("IR schema 不匹配")
    settings = data.get('settings', {})
    if 'writeFormat' in settings and settings['writeFormat'] not in ('ascii', 'binary'):
        raise ValueError('writeFormat 必须为 ascii 或 binary')
    objects = data.get("objects")
    materials = data.get("materials")
    if not isinstance(objects, list) or not objects or not isinstance(materials, dict):
        raise ValueError("IR 需要非空 objects 及 materials 对象")
    ids = set()
    coverage = {key: 0 for key in MAPPINGS}
    total_power = 0.0
    for obj in objects:
        oid = obj.get("id")
        if not isinstance(oid, str) or not oid or oid in ids:
            raise ValueError("对象 ID 空或重复")
        ids.add(oid)
        mapping = obj.get("mapping")
        if mapping not in MAPPINGS:
            raise ValueError("对象 mapping 无效")
        coverage[mapping] += 1
        material = obj.get("material")
        if 'flow_device' not in obj and material not in materials:
            raise ValueError(f"对象 {oid} 引用未知材料")
        validate_device(obj)
        validate_shape(obj.get('shape'))
        validate_enclosure(obj)
        validate_object(obj)
        if 'plate_thickness_override_m' in obj:
            if obj.get('shape',{}).get('kind') != 'flat-plate':
                raise ValueError('只有薄板可以覆盖等效厚度')
            if _finite(obj['plate_thickness_override_m'],f'{oid}.plate_thickness_override_m') <= 0:
                raise ValueError('覆盖薄板厚度必须是正数')
        if 'pcb' in obj:
            board = obj['pcb']
            if (not isinstance(board,dict) or board.get('model') not in ('supplied-effective-material','volume-fraction-laminate')
                    or not _coordinates(board.get('normal'),3) or not math.isclose(math.hypot(*board['normal']),1,rel_tol=1e-9)):
                raise ValueError('PCB 需要已声明的等效材料模型和单位法向')
        total_power += _finite(obj.get("power_w", 0), f"{oid}.power_w")
    for material_id, material in materials.items():
        if not material_id:
            raise ValueError("材料 ID 为空")
        conductivity = material.get("conductivity_w_mk")
        if isinstance(conductivity, (int, float)):
            _finite(conductivity, f"材料 {material_id} 导热率", 1e-12)
        elif isinstance(conductivity, list):
            if len(conductivity) != 3:
                raise ValueError("正交各向异性导热率必须为三分量")
            for value in conductivity:
                _finite(value, f"材料 {material_id} 导热率", 1e-12)
        else:
            raise ValueError("材料 conductivity_w_mk 无效")
        if 'conductivity_axes' in material:
            if not isinstance(conductivity,list):
                raise ValueError('仅正交各向异性材料接受导热主轴')
            axes = material['conductivity_axes']
            unit_axes(axes)
            if any(not math.isclose(math.hypot(*axis),1,rel_tol=1e-9) for axis in axes):
                raise ValueError('导热主轴必须是单位向量')
    for link in data.get("associations", []):
        if link.get("parent") not in ids or link.get("child") not in ids:
            raise ValueError("assembly 关联引用未知对象")
        if link["parent"] == link["child"]:
            raise ValueError("assembly 不允许自引用")
    children = {}
    for link in data.get("associations", []):
        children.setdefault(link["parent"], []).append(link["child"])
    visiting, visited = set(), set()
    def visit(oid):
        if oid in visiting:
            raise ValueError("assembly 关联存在循环")
        if oid in visited:
            return
        visiting.add(oid)
        for child in children.get(oid, []):
            visit(child)
        visiting.remove(oid)
        visited.add(oid)
    for oid in ids:
        visit(oid)
    paired_ids, paired_offsets = set(), set()
    for row in data.get('source_associations', []):
        oid, offset = row.get('object'), row.get('pdml_offset')
        if (oid not in ids or oid in paired_ids or type(offset) is not int or offset < 0
                or offset in paired_offsets):
            raise ValueError('ECXML/PDML 来源关联必须引用唯一的有效对象和记录偏移')
        paired_ids.add(oid)
        paired_offsets.add(offset)
    for instance in data.get('assembly', {}).get('xtxml', {}).get('instances', []):
        if instance.get('name_match') is not None and instance['name_match'] not in ids:
            raise ValueError('XTXML 装配实例引用了未知的几何对象')
    monitor_ids = set()
    for monitor in data.get("monitors", []):
        if monitor.get("type") not in {"region-temperature", "wall-heat-flux", "channel-flow", "probe"}:
            raise ValueError("monitor 类型无效")
        sources = monitor.get('sources')
        if sources is not None:
            locations = monitor.get('locations')
            if (monitor['type'] != 'probe' or not _observation_name(monitor) or monitor['id'] in monitor_ids
                    or not isinstance(sources, list) or not sources or not all(map(_observation_source, sources))
                    or not isinstance(locations, list) or len(locations) != 1 or not _coordinates(locations[0], 3)):
                raise ValueError('原始探针的位置或来源无效')
            monitor_ids.add(monitor['id'])
        targets = monitor.get("objects")
        if (not isinstance(targets, list) or any(target not in ids for target in targets)
                or not targets and not (sources and isinstance(monitor.get('reason'), str) and monitor['reason'])):
            raise ValueError("monitor 对象引用无效")
    region_ids = set()
    for region in data.get('regions', []):
        bounds = region.get('bounds_m')
        if (not _observation_name(region) or region['id'] in region_ids
                or region.get('type') not in {'domain', 'mesh-region'} or not _observation_source(region.get('source'))
                or not _coordinates(bounds, 6) or any(bounds[i] >= bounds[i+3] for i in range(3))):
            raise ValueError('原始区域的坐标或来源无效')
        if 'origin_m' in region or 'edge_vectors_m' in region:
            edges = region.get('edge_vectors_m')
            if (not _coordinates(region.get('origin_m'), 3) or not isinstance(edges, list) or len(edges) != 3
                    or not all(_coordinates(vector, 3) for vector in edges)):
                raise ValueError('原始区域的有向边无效')
        region_ids.add(region['id'])
    source = data.get("source", {})
    if not isinstance(source.get("path"), str) or len(source.get("sha256", "")) != 64:
        raise ValueError("IR source 路径/哈希无效")
    expected = {"objects": len(objects), **coverage, "power_w": total_power}
    if data.get("coverage") != expected:
        raise ValueError(f"coverage 不一致，应为 {expected}")
    return data


def build_ir(source_path: str, source_bytes: bytes, objects: list[dict], materials: dict,
             associations: list[dict] | None = None, monitors: list[dict] | None = None) -> dict:
    coverage = {key: 0 for key in MAPPINGS}
    normalized = []
    for obj in objects:
        item = dict(obj)
        item.setdefault("mapping", "missing-info")
        item["power_w"] = item.get("power_w", 0)
        if item["mapping"] in MAPPINGS:
            coverage[item["mapping"]] += 1
        normalized.append(item)
    data = {"schema": SCHEMA, "objects": normalized, "materials": materials,
            "assembly": {"roots": sorted({obj["id"] for obj in normalized} -
                                         {link["child"] for link in (associations or [])})},
            "associations": associations or [], "monitors": monitors or [],
            "coverage": {"objects": len(normalized), **coverage,
                         "power_w": sum(obj.get("power_w", 0) for obj in normalized)},
            "source": {"path": source_path, "sha256": hashlib.sha256(source_bytes).hexdigest()}}
    return validate_ir(data)


def write_ir(root: Path, data: dict) -> Path:
    validate_ir(data)
    root.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=root, prefix=".ir-", delete=False) as handle:
        handle.write(payload)
        temp = Path(handle.name)
    os.replace(temp, root / "flotherm-ir.json")
    return root / "flotherm-ir.json"


def read_ir(root: Path) -> dict:
    return validate_ir(json.loads((root / "flotherm-ir.json").read_text(encoding="utf-8")))
