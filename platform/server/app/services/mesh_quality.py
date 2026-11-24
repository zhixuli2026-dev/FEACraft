"""checkMesh 指标解析、质量报告与阶段门控（文件事实源）。"""
import hashlib
import json
import math
import re
from datetime import datetime, timezone

from .mesh_presets import effective_targets, selected as quality_preset

QUALITY_REPORT = "B-mesh/quality.json"
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_PATTERNS = {
    "non_orthogonality": re.compile(r"Mesh non-orthogonality Max:\s*(" + _NUM + ")"),
    "skewness": re.compile(r"Max skewness\s*=\s*(" + _NUM + ")"),
    "aspect_ratio": re.compile(r"Max aspect ratio\s*=\s*(" + _NUM + ")"),
    "jacobian": re.compile(r"Min(?:imum)? Jacobian\s*[:=]\s*(" + _NUM + ")"),
    "minimum_volume": re.compile(r"\bMin volume\s*=\s*(" + _NUM + ")"),
}
_TARGETS = {
    "non_orthogonality": "maxNonOrtho",
    "skewness": "maxSkewness",
    "aspect_ratio": "maxAspectRatio",
    "jacobian": "minJacobian",
    "minimum_volume": "minVolume",
}
_MINIMUM_METRICS = {"jacobian", "minimum_volume"}


def mesh_block_hash(mesh: dict) -> str:
    canonical = json.dumps(mesh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_metrics(text: str) -> dict:
    metrics = {}
    for name, pattern in _PATTERNS.items():
        matches = list(pattern.finditer(text))
        if matches:
            metrics[name] = float(matches[-1].group(1))
    return metrics


def evaluate_mesh_quality(log_text: str, targets: dict, *, measured_jacobian=None,
                          registered=False, include_volume=True) -> dict:
    metrics = parse_metrics(log_text)
    if not include_volume:
        metrics.pop("minimum_volume", None)
    if registered:
        metrics.pop("jacobian", None)
        if measured_jacobian is not None:
            metrics["jacobian"] = measured_jacobian
    violations = []
    for metric, target_key in _TARGETS.items():
        if target_key not in targets or (metric == "minimum_volume" and not include_volume):
            continue
        target = float(targets[target_key])
        if metric not in metrics:
            violations.append({"metric": metric, "reason": "missing_metric", "target": target})
            continue
        actual = metrics[metric]
        minimum = metric in _MINIMUM_METRICS
        failed = actual < target if minimum else actual > target
        if failed:
            violations.append({"metric": metric, "actual": actual, "target": target,
                               "reason": "below_minimum" if minimum else "above_maximum"})
    return {
        "schema": "vibeflow.mesh-quality.v1",
        "metrics": metrics,
        "violations": violations,
        "status": "fail" if violations else "pass",
        "policy": targets.get("policy", "fail"),
        "mesh_ok": bool(re.search(r"\bMesh OK\b", log_text)) and "FOAM FATAL" not in log_text,
    }


def read_quality_report(prefix: str) -> dict | None:
    from .. import storage
    if not storage.exists(prefix, QUALITY_REPORT):
        return None
    try:
        report = json.loads(storage.get_bytes(prefix, QUALITY_REPORT).decode("utf-8"))
        return report if isinstance(report, dict) else None
    except (UnicodeError, json.JSONDecodeError):
        return None


def write_quality_report(prefix: str, report: dict) -> dict:
    from .. import storage
    storage.put_text(prefix, QUALITY_REPORT, json.dumps(report, ensure_ascii=False))
    return report


def professional_quality_reason(report, targets):
    """A waiver may accept measured violations, never absent/invalid evidence."""
    if report.get('mesh_ok') is not True:
        return 'checkMesh did not prove Mesh OK'
    for key in _TARGETS.values():
        if key in targets and (not math.isfinite(float(targets[key])) or float(targets[key]) <= 0):
            return 'Quality targets must be finite and positive'
    metrics = report.get('metrics', {})
    if any(name not in metrics for name in ('non_orthogonality', 'skewness', 'aspect_ratio')):
        return 'checkMesh omitted required mesh metrics'
    if any(not math.isfinite(value) or (name != 'jacobian' and value < 0)
           for name,value in metrics.items()):
        return 'Mesh metrics are not valid finite measurements'
    if any(key in targets and metric not in metrics for metric,key in _TARGETS.items()):
        return 'A configured quality metric is missing'
    policy = targets.get('policy', 'fail')
    if policy not in {'fail', 'warn', 'allowWithReason'}:
        return 'Unknown mesh quality policy'
    if report.get('status') == 'fail' and policy != 'warn':
        if policy != 'allowWithReason' or not str(targets.get('reason', '')).strip():
            return 'Mesh quality failed without an applicable waiver'
    return None


def gate_decision(report, targets):
    reason=professional_quality_reason(report,targets)
    if reason:
        return {'status':'blocked','reason':reason}
    if report['status']=='pass':
        return {'status':'pass'}
    if report['policy']=='warn':
        return {'status':'warn'}
    return {'status':'allowed_with_reason','reason':targets['reason'].strip()}


def mesh_gate_reason(prefix: str, mesh: dict, policy: str, reason: str | None) -> str | None:
    report = read_quality_report(prefix)
    expected_hash = mesh_block_hash(mesh)
    if report is None or report.get("case_dict_mesh_hash") != expected_hash:
        return "缺少与当前 mesh 参数一致的质量报告，请先执行 mesh:evaluate"
    preset=quality_preset(mesh)
    if preset is not None and (report.get('quality_preset')!=preset or report.get('quality_targets')!=effective_targets(mesh)):
        return '网格报告与当前质量预设不一致，请重新评估'
    measured = professional_quality_reason(report, {**effective_targets(mesh),
        'policy':policy, 'reason':reason})
    if measured:
        return measured
    if report.get("status") != "fail" or policy == "warn":
        return None
    if policy == "allowWithReason" and reason and reason.strip():
        return None
    return f"网格质量未通过（{len(report.get('violations', []))} 项）且 policy={policy}"
