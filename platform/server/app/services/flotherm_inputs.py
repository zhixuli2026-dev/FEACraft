"""Flotherm 输入组合、版本、配对名称与 bbox 唯一性检查。"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

_FORMATS = {".ecxml": "ECXML", ".pdml": "PDML", ".xtxml": "XTXML",
            ".xtxmla": "XTXMLA", ".pack": "Pack", ".floscript": "FloSCRIPT"}
MAX_INPUT_BYTES = 32 * 1024 * 1024
_PDML_HEADER = re.compile(rb"#FFFB[ \t]+(V[0-9]{4})(?=[ \t\r\n]|$)", re.IGNORECASE)


def _bbox(root) -> list[list[float]]:
    values = []
    for element in root.iter():
        candidates = []
        if element.tag.lower().split("}")[-1] in {"bbox", "boundingbox"}:
            candidates.append(element.text or "")
        candidates.extend(element.attrib.get(name, "") for name in ("bbox", "boundingBox", "boundingbox"))
        for text in candidates:
            try:
                numbers = [float(item) for item in re.split(r"[\s,;]+", text.strip()) if item]
            except ValueError:
                continue
            if len(numbers) == 6 and all(math.isfinite(item) for item in numbers):
                if numbers[0] < numbers[3] and numbers[1] < numbers[4] and numbers[2] < numbers[5]:
                    values.append(numbers)
    return values


def _inspect_body(path, fmt, data, blockers):
    if fmt in {'Pack', 'FloSCRIPT'}:
        return []
    if fmt == 'PDML':
        header = _PDML_HEADER.match(data)
        if header is not None:
            if header[1].upper() != b'V2310':
                blockers.append(f"{path.name} 不是可确认的 V2310 PDML")
            return []
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, LookupError, ValueError):
        detail = '不是可确认的 V2310 PDML' if fmt == 'PDML' else 'XML 格式无效'
        blockers.append(f'{path.name} {detail}')
        return []
    if fmt == 'PDML' and (root.tag.rsplit('}', 1)[-1].lower() != 'pdml'
                          or root.get('version', '').upper() != 'V2310'):
        blockers.append(f"{path.name} 不是可确认的 V2310 PDML")
    return _bbox(root)


def inspect_inputs(paths: list[Path], studio: bool = True) -> dict:
    if not paths:
        raise ValueError("至少需要一个输入文件")
    files = []
    blockers = []
    duplicates = []
    seen = set()
    for path in paths:
        path = path.resolve()
        fmt = _FORMATS.get(path.suffix.lower())
        if fmt is None:
            raise ValueError(f"不支持的输入格式: {path.name}")
        if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError(f"输入不存在或超过 32 MiB: {path.name}")
        identity = (path.parent, path.stem.lower(), fmt)
        if identity in seen:
            raise ValueError(f"重复输入: {path.name}")
        seen.add(identity)
        with path.open('rb') as stream:
            data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(f"输入超过 32 MiB: {path.name}")
        boxes = _inspect_body(path, fmt, data, blockers)
        groups = {}
        for box in boxes:
            groups[tuple(box)] = groups.get(tuple(box), 0) + 1
        duplicates.extend({'file': path.name, 'bbox': list(box), 'count': count}
                          for box, count in groups.items() if count > 1)
        files.append({"name": path.name, "stem": path.stem, "format": fmt,
                      "sha256": hashlib.sha256(data).hexdigest(), 'bbox_count':len(boxes)})
    warnings = []
    formats = {item["format"] for item in files}
    stems = {item["stem"].lower() for item in files}
    kind = None
    if formats == {"ECXML"}:
        kind = "ECXML"
    elif formats == {"PDML"}:
        kind = "PDML"
    elif formats == {"ECXML", "PDML"}:
        kind = "ECXML+PDML"
    elif formats in ({"XTXML", "PDML"}, {"XTXMLA", "PDML"},
                     {"XTXML", "PDML", "ECXML"}, {"XTXMLA", "PDML", "ECXML"}):
        kind = next(iter(formats & {'XTXML','XTXMLA'})) + '+PDML'
    elif formats in ({"Pack"}, {"FloSCRIPT"}):
        kind = next(iter(formats))
        if studio:
            blockers.append(f"Studio 不支持 .{kind.lower()}，请使用 ECXML/PDML 输入组合")
    else:
        blockers.append(f"不支持的输入组合: {sorted(formats)}")
    if len(files) > 1 and len(stems) != 1:
        blockers.append("组合输入必须具有相同主文件名（不区分大小写）")
    if len(stems) == 1 and len(formats) != len(files):
        blockers.append("同一组合不能包含多个同类型输入")
    if duplicates:
        warnings.append("同一文件存在重复 bbox，转换前需确认对象唯一性")
    return {"schema": "vibeflow.flotherm.input-inspection.v1", "kind": kind,
            "ok": not blockers, "files": files, "blockers": blockers,
            "warnings": warnings, "bbox_duplicates": duplicates, "studio": studio}
