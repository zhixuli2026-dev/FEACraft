"""报告前的后处理证据门禁；只读文件，不创建作业或目录。"""
import asyncio
import hashlib
import json

from .. import storage
from .casedict import CASE_DICT_PATH, parse_casedict
from .postprocess_executor import config_from_case_dict, postprocess_fingerprint, reference_input_paths
from .case_evidence import case_input_fingerprint, is_run_input, matches_current_sha256, verify_records
from .file_read import FileReadConflict


def _needs_evidence(config: dict) -> bool:
    operations = ("pointProbes", "lineSamples", "surfaceIntegrals", "volumeAverages",
                  "frequencySpectra", "modalQuantities", "referenceFields", "formulas", "uncertainty")
    return any(config.get(name) for name in operations) or bool(
        config.get("comparisonTargets") or config.get("plots") or config.get("referenceValues"))


def report_gate_reason(case) -> str | None:
    """未配置后处理时兼容旧报告；配置后要求同指纹且 report_ready 的证据。"""
    try:
        current_bytes = storage.get_bytes(case.s3_prefix, CASE_DICT_PATH)
        current_text = current_bytes.decode("utf-8")
        parsed = parse_casedict(current_text)
        config = config_from_case_dict(parsed)
    except Exception as error:
        return f"caseDict.postProcessing 无法用于报告门禁: {error}"
    if not _needs_evidence(config):
        return None
    expected = postprocess_fingerprint(config)
    pointer_path = "D-post/data/latest-postprocess.json"
    if not storage.exists(case.s3_prefix, pointer_path):
        return "已配置后处理，但没有成功的指标证据；请先运行 postprocess:run"
    try:
        pointer = json.loads(storage.get_bytes(case.s3_prefix, pointer_path))
        job_id = int(pointer["job_id"])
        if job_id <= 0:
            raise ValueError("invalid job id")
        metrics_path = f"D-post/data/postprocess-{job_id}/metrics.json"
        manifest_path = f"D-post/data/postprocess-{job_id}/artifact-manifest.json"
        metrics = json.loads(storage.get_bytes(case.s3_prefix, metrics_path))
        manifest = json.loads(storage.get_bytes(case.s3_prefix, manifest_path))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        return f"后处理结果指针或 metrics 损坏: {error}"
    if not isinstance(metrics, dict) or not isinstance(manifest, dict):
        return "后处理指标或产物清单格式损坏，请重新运行后处理"
    source = metrics.get("source") or {}
    if not isinstance(source, dict):
        return "后处理来源证据格式损坏，请重新运行后处理"
    if source.get("postprocess_sha256") != expected:
        return "postProcessing 已变化，现有证据已过期；请重新运行 postprocess:run"
    source_fingerprint = source.get("case_input_sha256")
    if source_fingerprint:
        if source_fingerprint != case_input_fingerprint(current_text):
            return "工程输入参数已变化，现有证据已过期；请重新运行求解与后处理"
    elif source.get("sha256") != hashlib.sha256(current_bytes).hexdigest():
        return "缺少与当前 caseDict 对应的工程输入证据，请重新运行 postprocess:run"
    reason = verify_records(case.s3_prefix, manifest.get("inputs"), label="计算输入", required=True)
    if reason:
        return reason
    reason = verify_records(case.s3_prefix, manifest.get("artifacts"), label="后处理产物", required=True)
    if reason:
        return reason
    if "run_inventory" in manifest:
        try:
            current_inventory = sorted(item["path"] for item in storage.list_tree(
                case.s3_prefix, config["run_dir"]) if is_run_input(item["path"], config["run_dir"]))
        except (OSError, ValueError):
            return "无法核对求解输入目录，请重新运行 postprocess:run"
        if current_inventory != manifest["run_inventory"]:
            return "求解输入或结果时刻目录已变化，现有证据已过期；请重新运行后处理"
    input_hashes = {item.get("path"): item.get("sha256") for item in manifest.get("inputs", [])}
    try:
        paths = reference_input_paths(config)
    except (ValueError, TypeError) as error:
        return f"参考文件无法用于报告门禁: {error}"
    for path in paths:
        try:
            expected_hash = input_hashes.get(path)
            if not expected_hash:
                return f"参考文件未纳入证据清单: {path}"
            matches = matches_current_sha256(case.s3_prefix, path, expected_hash)
        except FileReadConflict:
            matches = False
        except Exception as error:
            return f"参考文件无法用于报告门禁: {error}"
        if not matches:
            return f"参考文件已变化，现有证据已过期: {path}；请重新运行 postprocess:run"
    readiness = metrics.get("readiness") or {}
    if not isinstance(readiness, dict):
        return "后处理 readiness 格式损坏，请重新运行后处理"
    if readiness.get("report_ready") is not True:
        blockers = readiness.get("blockers") or ["report_ready=false"]
        return "后处理 readiness 未满足: " + "；".join(str(item) for item in blockers)
    return None


async def authoritative_reason(db,case):
    from .postprocess_publication import gate_reason
    from .async_ops import finish_before_cancel
    reason,handled=await gate_reason(db,case)
    return reason if handled else await finish_before_cancel(asyncio.to_thread(report_gate_reason,case))
