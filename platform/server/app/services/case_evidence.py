"""Shared engineering-input fingerprints and OpenFOAM file inventories.

No database or application startup dependency: geometry/postprocess workers also
use these functions. Workflow bookkeeping must not invalidate a completed solve.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .casedict import parse_casedict

_TIME = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def case_input_fingerprint(text: str) -> str:
    parsed = parse_casedict(text)
    for name in ("workflowState", "errors", "provenance", "report", "postProcessing"):
        parsed.pop(name, None)
    meta = parsed.get("caseMeta")
    if isinstance(meta, dict):
        meta.pop("status", None)
    content = json.dumps(parsed, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def safe_run_dir(value: str) -> str:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts or
            len(path.parts) < 2 or path.parts[0] != "C-run"):
        raise ValueError("运行目录必须是 C-run 下的相对算例目录")
    return path.as_posix()


def is_run_input(path: str, run_dir: str, *, results: bool = True) -> bool:
    prefix = safe_run_dir(run_dir) + "/"
    if not path.startswith(prefix):
        return False
    parts = path[len(prefix):].split("/")
    if len(parts) < 2:
        return False
    name = parts[0]
    if name in ("system", "constant", "0", "0.orig"):
        return True
    return bool(results and _TIME.fullmatch(name) and math.isfinite(float(name)))


def run_inventory(case_root: Path, run_dir: str, *, results: bool = True) -> list[Path]:
    root = case_root.resolve()
    run = (root / safe_run_dir(run_dir)).resolve()
    if not run.is_relative_to(root):
        raise ValueError("运行目录越过 case 根目录")
    files = []
    for path in run.rglob("*"):
        if path.is_file() and is_run_input(path.relative_to(root).as_posix(), run_dir, results=results):
            if not path.resolve().is_relative_to(root):
                raise ValueError("运行输入符号链接越过 case 根目录")
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def file_record(case_root: Path, path: Path) -> dict:
    case_root, path = case_root.resolve(), path.resolve()
    relative = path.relative_to(case_root).as_posix()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {"path": relative,
            "size": path.stat().st_size, "sha256": digest.hexdigest()}


def matches_current_sha256(prefix: str, path: str, expected: str) -> bool:
    from .. import storage
    from .file_read import FileRead
    from .local_source_reads import observe

    with observe(storage._fs_path(prefix,'.'),[path]):
        reader = FileRead(prefix, path)
        actual = hashlib.sha256()
        with closing(reader.chunks()) as chunks:
            for block in chunks:
                actual.update(block)
        return actual.hexdigest() == expected and FileRead(prefix, path).version == reader.version


def _changed_record(label, path):
    return f"{label}文件已变化，现有证据已过期: {path}；请重新计算"


def _verify_object_records(prefix, files, label):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from itertools import islice

    from .attempt_objects import selected_digest
    from .case_read_view import remote_file_versions
    from .case_write import CaseWriteConflict

    paths = tuple(files)
    versions = remote_file_versions(prefix,paths)

    def verify_one(path):
        observed = versions[path]
        if observed is None:
            return f"{label}文件缺失或不可读，现有证据已过期: {path}"
        try:
            digest = selected_digest(prefix,path,**observed)
        except CaseWriteConflict:
            return _changed_record(label,path)
        except TimeoutError:
            raise
        except (OSError,ValueError,KeyError):
            return f"{label}文件缺失或不可读，现有证据已过期: {path}"
        if digest['sha256']!=files[path]:
            return _changed_record(label,path)

    remaining = iter(paths)
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='engineering-object-read') as readers:
        while group := tuple(islice(remaining,4)):
            tasks = [readers.submit(copy_context().run,verify_one,path) for path in group]
            for task in tasks:
                if reason := task.result():
                    return reason
    after = remote_file_versions(prefix,paths)
    for path in paths:
        if versions[path]!=after[path]:
            return _changed_record(label,path)
    return None


def verify_records(prefix: str, records, *, label: str, required: bool = False,
                   parallel_remote: bool = False) -> str | None:
    from .. import storage
    from .attempt_workspace import _reserved
    from .file_read import FileReadConflict

    if not isinstance(records, list) or (required and not records):
        return f"{label}证据清单为空或损坏，请重新计算"
    seen = {}
    for record in records:
        if not isinstance(record, dict):
            return f"{label}证据清单损坏"
        path = str(record.get("path", ""))
        digest = record.get("sha256", "")
        if (not path or path in seen or PurePosixPath(path).is_absolute() or
                PureWindowsPath(path).drive or ".." in path.replace("\\", "/").split("/") or
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            return f"{label}证据路径或哈希不合法"
        seen[path] = digest
    if parallel_remote and seen and storage._use_s3() and not any(_reserved(path) for path in seen):
        return _verify_object_records(prefix,seen,label)
    for path,digest in seen.items():
        changed = _changed_record(label,path)
        try:
            if not matches_current_sha256(prefix, path, digest):
                return changed
        except FileReadConflict:
            return changed
        except TimeoutError:
            raise
        except (OSError, ValueError, KeyError):
            return f"{label}文件缺失或不可读，现有证据已过期: {path}"
    return None


def capture_solve_inputs(case_root: Path, run_dir: str, job_id: int, *, regions=()) -> dict:
    case_root = case_root.resolve()
    run_dir = safe_run_dir(run_dir)
    required = [f"{run_dir}/system/controlDict"]
    if regions:
        from .casedict_schema import valid_cell_zone
        if any(not valid_cell_zone(name) for name in regions):
            raise ValueError('Invalid names in the OpenFOAM solve mesh inventory')
        required.extend(f'{run_dir}/constant/{region}/polyMesh/{name}' for region in regions
                        for name in ('points','faces','owner','neighbour','boundary'))
    else:
        required.extend(f'{run_dir}/constant/polyMesh/{name}' for name in ('points','faces','owner','boundary'))
    if any(not (case_root / path).is_file() for path in required):
        raise ValueError("求解证据需要 controlDict 和已生成的 OpenFOAM 网格")
    text = (case_root / "0-caseDict/caseDict").read_text(encoding="utf-8")
    return {"schema": "vibeflow.solve-evidence.v1", "job_id": job_id,
            "run_dir": run_dir, "case_input_sha256": case_input_fingerprint(text),
            "inputs": [file_record(case_root, path)
                       for path in run_inventory(case_root, run_dir, results=False)]}


def complete_solve_evidence(case_root: Path, snapshot: dict, log_path: Path) -> dict:
    from .logparse import parse_solver_log
    case_root = case_root.resolve()
    summary = parse_solver_log(log_path.read_text(encoding="utf-8", errors="replace"))["summary"]
    if (not summary["finished_cleanly"] or summary["fatal_errors"] or
            not summary["fields"] or summary["last_time"] is None):
        raise ValueError("没有正常收尾且含求解残差的 OpenFOAM 日志，不能作为求解完成证据")
    text = (case_root / "0-caseDict/caseDict").read_text(encoding="utf-8")
    if case_input_fingerprint(text) != snapshot["case_input_sha256"]:
        raise ValueError("求解期间工程参数已变化，结果不可用于阶段推进")
    inputs = [file_record(case_root, path) for path in
              run_inventory(case_root, snapshot["run_dir"], results=False)]
    if inputs != snapshot["inputs"]:
        raise ValueError("求解期间输入或网格已变化，结果不可用于阶段推进")
    outputs = []
    for path in run_inventory(case_root, snapshot["run_dir"]):
        relative = path.relative_to(case_root).as_posix()
        if not is_run_input(relative, snapshot["run_dir"], results=False):
            outputs.append(file_record(case_root, path))
    if not outputs:
        raise ValueError("没有求解场产物，不能作为求解完成证据")
    latest = float(summary["last_time"])
    if not any(math.isclose(float(PurePosixPath(record["path"]).relative_to(
            snapshot["run_dir"]).parts[0]), latest, abs_tol=1e-9, rel_tol=0)
               for record in outputs):
        raise ValueError("日志末时刻没有对应的求解场产物")
    return {**snapshot, "summary": summary, "outputs": outputs,
            "log": file_record(case_root, log_path)}


def solve_gate_reason(prefix: str, job_id: int, run_dir: str | None = None) -> str | None:
    from .. import storage
    try:
        evidence = json.loads(storage.get_bytes(prefix, f".vibeflow/jobs/job-{job_id}-evidence.json"))
        text = storage.get_bytes(prefix, "0-caseDict/caseDict").decode("utf-8")
    except (OSError, ValueError, KeyError):
        return "缺少可追溯的求解证据；请用 run_dir 指定算例目录重新求解"
    if (not isinstance(evidence, dict) or evidence.get("schema") != "vibeflow.solve-evidence.v1" or
            evidence.get("job_id") != job_id or
            evidence.get("case_input_sha256") != case_input_fingerprint(text)):
        return "求解证据与当前工程输入不一致，请重新求解"
    if run_dir and evidence.get("run_dir") != run_dir:
        return "求解证据不属于当前后处理运行目录"
    for name in ("inputs", "outputs"):
        reason = verify_records(prefix, evidence.get(name), label="求解" + name, required=True)
        if reason:
            return reason
    return verify_records(prefix, [evidence.get("log")], label="求解日志", required=True)
