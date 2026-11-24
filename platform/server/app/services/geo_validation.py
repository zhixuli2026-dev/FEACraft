"""CadQuery/OCCT 几何有效性与边拓扑验证产物。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .case_evidence import case_input_fingerprint
from .casedict import parse_casedict

SCHEMA = "vibeflow.geometry.validation.v1"
DECLARATION_FIELDS = ("sourceType", "topology", "scaleToMeters", "symmetryUsed", "periodicUsed",
                      "watertightRequired", "manifoldRequired")


def geometry_declarations(text):
    geometry = parse_casedict(text).get("geometry", {})
    if not isinstance(geometry, dict):
        return {}
    return {key: geometry[key] for key in DECLARATION_FIELDS
            if isinstance(geometry.get(key), (str, bool, int, float))}


def topology_requirement_reason(declarations, summary):
    missing = [label for field, result, label in (
        ("watertightRequired", "watertight", "水密"),
        ("manifoldRequired", "manifold", "流形"),
    ) if str(declarations.get(field)).lower() == "true" and summary.get(result) is not True]
    if missing:
        return f"算例要求几何满足{'、'.join(missing)}条件。请核对当前几何验证，再进入网格阶段"
    return None


def validate_shape(shape) -> dict:
    bb = shape.BoundingBox()
    edges = list(shape.Edges())
    face_counts = [len(edge.ancestors(shape, "Face").Faces()) for edge in edges]
    boundary = sum(count == 1 for count in face_counts)
    nonmanifold = sum(count > 2 for count in face_counts)
    solids = len(shape.Solids())
    valid = bool(shape.isValid())
    return {
        "valid": valid,
        "volume": float(shape.Volume()),
        "area": float(shape.Area()),
        "bbox": {"min": [bb.xmin, bb.ymin, bb.zmin],
                 "max": [bb.xmax, bb.ymax, bb.zmax]},
        "counts": {"solids": solids, "shells": len(shape.Shells()),
                   "faces": len(shape.Faces()), "edges": len(edges),
                   "vertices": len(shape.Vertices())},
        "topology": {"boundary_edges": boundary, "nonmanifold_edges": nonmanifold,
                     "shared_edges": sum(count == 2 for count in face_counts),
                     "face_adjacency": face_counts},
        "watertight": valid and solids > 0 and boundary == 0 and nonmanifold == 0,
        "manifold": nonmanifold == 0,
    }


def write_validation(case_root: Path, display: list[dict], tolerance: float) -> dict:
    a_geo = case_root / "A-geo"
    objects = []
    for item in display:
        try:
            shape = item["object"] if hasattr(item["object"], "wrapped") else item["object"].val()
            result = {"name": item["name"], **validate_shape(shape)}
        except Exception as error:
            result = {"name": item["name"], "valid": False, "watertight": False,
                      "manifold": False, "error": f"{type(error).__name__}: {error}"}
        objects.append(result)
    script = a_geo / "geometry.py"
    case_dict = case_root / "0-caseDict" / "caseDict"
    dictionary = case_dict.read_bytes()
    text = dictionary.decode("utf-8")
    report = {
        "schema": SCHEMA,
        "source_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "casedict_sha256": hashlib.sha256(dictionary).hexdigest(),
        "case_input_sha256": case_input_fingerprint(text),
        "declared_geometry": geometry_declarations(text),
        "tolerance": tolerance,
        "objects": objects,
        "summary": {
            "valid": bool(objects) and all(item.get("valid") for item in objects),
            "watertight": bool(objects) and all(item.get("watertight") for item in objects),
            "manifold": bool(objects) and all(item.get("manifold") for item in objects),
            "object_count": len(objects),
        },
    }
    (a_geo / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report
