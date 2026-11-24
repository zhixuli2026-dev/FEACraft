"""OpenFOAM 补全索引服务：懒加载自建 JSON + 前缀/类型过滤"""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

_INDEX_PATH = Path(__file__).parent.parent.parent / "data" / "completion" / "openfoam-keywords.json"
_cache: dict | None = None
_load_lock = Lock()


def _index() -> dict | None:
    global _cache
    if _cache is not None:
        return _cache
    with _load_lock:
        if _cache is not None:
            return _cache
        try:
            data = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
            items = data["completionItems"]
            if (not isinstance(data["source"], str) or not isinstance(data["generated"], str)
                    or not isinstance(items, list) or not all(
                        isinstance(item["label"], str) and isinstance(item["kinds"], list)
                        and type(item["occurrences"]) is int for item in items)):
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        _cache = data
    return _cache


def reload_index():
    """重建后调用（测试/热更新用）"""
    global _cache
    with _load_lock:
        _cache = None


def search(prefix: str, kind: str = "", limit: int = 30, parent: str = "", context: str = "") -> dict:
    idx = _index()
    if idx is None:
        return {"ready": False, "items": []}
    p = prefix.lower()
    observed = None
    if parent and (kind == "dictionaryValue" or not kind and context):
        # A label's union of parents and contexts cannot prove a particular pair.
        for group in idx.get("dictionaryValuesByKey", []):
            if group["keyword"] == parent:
                observed = {value["value"]: value for value in group["values"]}
                break
    out = []
    for it in idx["completionItems"]:
        if p and not it["label"].lower().startswith(p):
            continue
        if kind and kind not in it["kinds"]:
            continue
        evidence = observed.get(it["label"]) if observed is not None else None
        if parent and (parent not in it.get("parents", []) or observed is not None and evidence is None):
            continue
        examples = evidence.get("contextExamples") if evidence is not None else None
        domains = examples if examples is not None else it.get("contexts", [])
        if context and context not in domains:
            continue
        sources = evidence["sourceExamples"] if evidence is not None else it.get("sourceExamples", [])
        if context and examples is not None:
            sources = [examples[context]]
        out.append({"label": it["label"], "kinds": it["kinds"],
                    "occurrences": it["occurrences"], "parents": it.get("parents", []),
                    "confidence": it.get("confidence", 0.5),
                    "contexts": sorted(domains), "sourceExamples": sources})
        if len(out) >= limit:
            break
    return {"ready": True, "source": idx["source"], "generated": idx["generated"],
            "generator_sha256": idx.get("generator_sha256"),
            "index_sha256": idx.get("index_sha256"),
            "baseline_sha256": idx.get("baseline", {}).get("sha256"),
            "total": len(idx["completionItems"]), "items": out}
