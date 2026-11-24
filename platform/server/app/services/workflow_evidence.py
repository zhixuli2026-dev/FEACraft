"""Fresh geometry and mesh evidence required by the stage transition API."""
import hashlib
import json

from .. import storage
from .case_evidence import case_input_fingerprint, verify_records
from .casedict import CASE_DICT_PATH, parse_casedict
from .file_read import FileRead, FileReadConflict
from .geo_validation import geometry_declarations, topology_requirement_reason
from .mesh_quality import read_quality_report, mesh_gate_reason
from .mesh_presets import effective_targets


def geometry_gate_reason(prefix: str, *, original_dictionary: str | None = None) -> str | None:
    try:
        validation = json.loads(FileRead(prefix, "A-geo/validation.json").read())
        current_text = (original_dictionary if original_dictionary is not None else
                        FileRead(prefix, CASE_DICT_PATH).read().decode("utf-8"))
        script_hash = hashlib.sha256(FileRead(prefix, "A-geo/geometry.py").read()).hexdigest()
    except (OSError, ValueError, KeyError, FileReadConflict):
        return "缺少有效几何验证，请先完成几何预览"
    if not isinstance(validation, dict):
        return "几何验证证据格式损坏，请重新预览"
    summary = validation.get("summary") or {}
    if not isinstance(summary, dict):
        return "几何验证摘要格式损坏，请重新预览"
    if not summary.get("valid") or not summary.get("object_count", 0):
        return "几何验证未通过或没有几何对象，不能进入网格阶段"
    expected = validation.get("case_input_sha256")
    matches = (expected == case_input_fingerprint(current_text)) if expected else (
        validation.get("casedict_sha256") == hashlib.sha256(current_text.encode()).hexdigest())
    if script_hash != validation.get("source_sha256") or not matches:
        return "几何脚本或工程参数已变化，请重新预览验证"
    return topology_requirement_reason(geometry_declarations(current_text), summary)


def mesh_evidence_gate_reason(prefix: str) -> str | None:
    try:
        text = storage.get_bytes(prefix, CASE_DICT_PATH).decode("utf-8")
        parsed = parse_casedict(text)
        report = read_quality_report(prefix)
        if not report or report.get("mesh_ok") is not True:
            return "缺少真实 checkMesh 的 Mesh OK 证据，请先执行 mesh:evaluate"
        if report.get("case_input_sha256") != case_input_fingerprint(text):
            return "网格报告与当前工程参数不一致，请重新验证网格"
        reason = verify_records(prefix, report.get("inputs"), label="网格输入", required=True)
        if reason:
            return reason
        reason = verify_records(prefix, [{"path": report.get("log_path"),
            "sha256": report.get("log_sha256")}], label="checkMesh 日志", required=True)
        if reason:
            return reason
        mesh = parsed.get("mesh") or {}
        targets = effective_targets(mesh)
        from .mesh_quality import evaluate_mesh_quality
        from .mesh_artifacts import MESH_FILES
        from .case_evidence import safe_run_dir
        recomputed = evaluate_mesh_quality(storage.get_bytes(prefix, report['log_path']).decode('utf-8','replace'),
            targets, include_volume='gate' in report)
        if any(report.get(key) != value for key,value in recomputed.items()):
            return '网格质量报告与实际日志不一致，请重新评估'
        required = {safe_run_dir(report['run_dir'])+'/constant/polyMesh/'+name for name in MESH_FILES}
        if not required.issubset({record['path'] for record in report['inputs']}):
            return '网格证据未包含完整 polyMesh 文件，请重新生成'
        return mesh_gate_reason(prefix, mesh, targets.get("policy", "fail"), targets.get("reason"))
    except (OSError, ValueError, KeyError, TypeError):
        return "网格证据损坏，请重新执行 mesh:evaluate"
