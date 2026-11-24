"""Flotherm 转换覆盖与功率平衡报告。"""
from __future__ import annotations

import json
from pathlib import Path
from html import escape

from .flotherm_ir import validate_ir

SCHEMA = "vibeflow.flotherm.coverage.v1"


def build_coverage_report(ir: dict) -> dict:
    validate_ir(ir)
    rows = []
    blockers = []
    mapped_power = 0.0
    unmapped_power = 0.0
    for obj in ir["objects"]:
        mapping = obj["mapping"]
        power = float(obj.get("power_w", 0))
        if mapping in {"converted", "equivalent"}:
            mapped_power += power
        else:
            unmapped_power += power
        reason = obj.get("reason") or obj.get("unsupported_reason")
        missing = obj.get("missing_fields") or []
        if mapping in {"unsupported", "missing-info"}:
            blockers.append({"object": obj["id"], "mapping": mapping,
                             "reason": reason or "缺少转换所需信息", "missing_fields": missing})
        rows.append({"id": obj["id"], "type": obj.get("type", "unknown"),
                     "mapping": mapping, "power_w": power,
                     "reason": reason, "missing_fields": missing})
    total = mapped_power + unmapped_power
    return {"schema": SCHEMA, "source": ir["source"], "objects": rows,
            "summary": {"objects": len(rows), "input_power_w": total,
                        "mapped_power_w": mapped_power, "unmapped_power_w": unmapped_power,
                        "balanced": abs(total - ir["coverage"]["power_w"]) < 1e-9},
            "monitors": ir["monitors"], "blockers": blockers}


def _cell(value):
    return escape(str(value)).replace('|', r'\|').replace('\n', ' ').replace('\r', ' ')


def source_trace(ir, manifest):
    assembly = ir.get('assembly', {})
    pairs = ir.get('source_associations', [])
    if not (pairs or ir.get('associations') or any(assembly.get(key) for key in ('groups', 'pdml_groups', 'xtxml'))):
        return {}
    targets = {row['id']: {'region': name} for name, row in manifest.get('solid_regions', {}).items()}
    for name, merged in manifest.get('merged_solid_regions', {}).items():
        targets.update({row['id']: {'region': name, 'cell_zone': row['cell_zone']} for row in merged['members']})
    targets.update({row['id']: {'region': manifest['fluid_region'], 'cell_zone': row['cell_zone']}
                    for row in manifest.get('flow_devices', [])})
    for row in manifest['heat_sources']:
        if row.get('zone'):
            targets.setdefault(row['id'], {})['cell_zone'] = row['zone']
    return {'assembly': assembly, 'associations': ir.get('associations', []), 'source_associations': pairs,
            'objects': [{'id': obj['id'], 'path': obj.get('path'), 'source': obj.get('source'),
                         'mapping': obj['mapping'], 'target': targets.get(obj['id'])} for obj in ir['objects']]}


def write_coverage_report(root: Path, ir: dict, manifest=None) -> Path:
    report = build_coverage_report(ir)
    for field in ('regions', 'monitor_coverage', 'refinements', 'merged_solid_regions', 'source_trace'):
        if manifest and field in manifest:
            report[field] = manifest[field]
    path = root / "conversion-coverage.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")
    lines = ["# Flotherm 转换覆盖报告", "",
             f"- 对象：{report['summary']['objects']}",
             f"- 输入功率：{report['summary']['input_power_w']:.6g} W",
             f"- 已映射功率：{report['summary']['mapped_power_w']:.6g} W",
             f"- 未映射功率：{report['summary']['unmapped_power_w']:.6g} W", "",
             "| 对象 | 类型 | 映射 | 功率/W | 原因 |", "|---|---|---|---:|---|"]
    for row in report["objects"]:
        lines.append(f"| {row['id']} | {row['type']} | {row['mapping']} | {row['power_w']:.6g} | {row['reason'] or ''} |")
    if report.get('monitor_coverage'):
        lines.extend(['', '## 原始温度探针', '', '| 路径 | 映射 | 输出或原因 |', '|---|---|---|'])
        for row in report['monitor_coverage']:
            lines.append(f"| {_cell(row['path'])} | {row['mapping']} | {_cell(row.get('function') or row['reason'])} |")
    if report.get('regions'):
        lines.extend(['', '## 原始区域', '', '| 路径 | 用途 | 坐标范围（m） |', '|---|---|---|'])
        for row in report['regions']:
            usage = {'domain':'计算域','refinement':f'网格细化 {row.get("refinement_level", 0)} 级'}.get(
                row['mapping'], '保留网格区域坐标')
            lines.append(f"| {_cell(row['path'])} | {usage} | {row['bounds_m']} |")
    if report.get('merged_solid_regions'):
        lines.extend(['','## 并盒成员','','| 固体区 | 对象 | 热源单元区 | 功率/W |','|---|---|---|---:|'])
        for name,model in report['merged_solid_regions'].items():
            for row in model['members']:
                lines.append(f'| {_cell(name)} | {_cell(row["id"])} | {_cell(row["cell_zone"])} | {row["power_w"]:.6g} |')
    (root / "conversion-coverage.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
