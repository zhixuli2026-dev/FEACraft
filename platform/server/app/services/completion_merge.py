"""把原版 31,542-label 基准与自有源码上下文合并，保持基准 label 集不变。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from .completion_builder import KIND_CONFIDENCE

_BASE_KIND_CONFIDENCE = {
    "header": 0.8, "macro": 0.85, "dictionaryKey": 0.9, "class": 0.95,
    "dictionaryValue": 0.75, "runtimeType": 1.0, "compoundDictionaryKey": 0.9,
    "dictionaryVariable": 0.8, "typeAlias": 0.95, "commandLineToken": 0.85,
    "enumValue": 0.8, "dictionaryFileName": 0.85, "enumType": 0.95,
    "compiledApplication": 1.0, "fieldFileName": 0.85, "namespace": 0.85,
    "struct": 0.95, "utility": 1.0, "sharedLibrary": 0.9, "openfoamCoreToken": 0.9,
    "solver": 1.0, "cppKeyword": 0.95, "environmentVariable": 0.8,
    "dictionaryDirective": 0.85, "foamFileClass": 0.95,
    "cppPreprocessorDirective": 0.9, "dictionaryLiteral": 0.75,
    "wmakeVariable": 0.8, "wmakeKeyword": 0.85,
}


def _confidence(kind: str) -> float:
    if kind in KIND_CONFIDENCE:
        return KIND_CONFIDENCE[kind]
    base = kind.split(":", 1)[0]
    value = _BASE_KIND_CONFIDENCE.get(base, 0.5)
    if kind.endswith(":source-backed"):
        value = min(1.0, value + 0.05)
    return value


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def merge_completion_items(baseline_items: list[dict], own_items: list[dict]) -> list[dict]:
    own_by_label = {item["label"]: item for item in own_items}
    merged = []
    for baseline in baseline_items:
        own = own_by_label.get(baseline["label"], {})
        kinds = sorted(set(baseline["kinds"]) | set(own.get("kinds", [])))
        merged.append({
            "label": baseline["label"], "kinds": kinds,
            "occurrences": own.get("occurrences", 1),
            "parents": own.get("parents", []),
            "confidence": max(_confidence(kind) for kind in kinds),
            "contexts": own.get("contexts", []),
            "sourceExamples": own.get("sourceExamples", []),
        })
    merged.sort(key=lambda item: (-item["confidence"], -item["occurrences"], item["label"]))
    return merged


def merge_index(baseline: dict, own: dict) -> dict:
    items = merge_completion_items(baseline["completionItems"], own["completionItems"])
    assert len(items) == len(baseline["completionItems"])
    assert {item["label"] for item in items} == {item["label"] for item in baseline["completionItems"]}
    out = dict(own)
    out["source"] = "OpenFOAM v2406 original label baseline + official source context merge"
    out["generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out["baseline"] = {
        "format": baseline.get("format"), "schemaVersion": baseline.get("schemaVersion"),
        "label_count": len(baseline["completionItems"]),
        "sha256": hashlib.sha256(_canonical(baseline["completionItems"])).hexdigest(),
    }
    out["builder_sha256"] = own.get("generator_sha256")
    out["stats"] = {**own.get("stats", {}), "completion_items": len(items)}
    out["completionItems"] = items
    canonical = _canonical({key: value for key, value in out.items()
                            if key not in ("generated", "generator_sha256", "index_sha256")})
    out["generator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    out["index_sha256"] = hashlib.sha256(canonical).hexdigest()
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("source_index", type=Path)
    parser.add_argument("output", type=Path, help="Write a new candidate index for validation")
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    own = json.loads(args.source_index.read_text(encoding="utf-8"))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(merge_index(baseline, own), stream, ensure_ascii=False)
