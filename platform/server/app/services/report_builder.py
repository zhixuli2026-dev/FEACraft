"""从 caseDict、网格质量和后处理证据组织报告章节。"""
import hashlib
from html import escape
import json
import math
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote

from .. import storage
from .casedict import CASE_DICT_PATH, parse_casedict
from .case_evidence import case_input_fingerprint
from .postprocess_gate import report_gate_reason
from .report_templates import frontmatter, profile


_CHAPTERS = {
    "problem": ("问题与验收", ("caseMeta", "intent", "physicalProblem"), ("acceptance",)),
    "assumptions": ("假设", ("intent",), ()),
    "geometry": ("几何与材料", ("geometry", "materials", "regions", "interfaces", "boundariesLoadsInitials"), ()),
    "mesh": ("网格与质量", ("mesh",), ("mesh_quality",)),
    "methods": ("数值方法、求解与执行", ("numericalMethod", "execution"), ("run",)),
    "results": ("后处理结果", ("postProcessing",), ("metrics", "figures")),
    "verification": ("Verification 与 Validation", ("postProcessing",), ("readiness", "comparisons")),
    "limitations": ("不确定度、限制与未决项", ("intent", "errors"), ("uncertainty",)),
    "evidence": ("证据索引与可复现性", ("report", "provenance"), ("case_revision", "postprocess_manifest")),
}


def _read(prefix, path, root, *, load=None):
    if load is not None and not load(path):
        return None
    if root is None and not storage.exists(prefix, path):
        return None
    try:
        if root is None:
            return storage.get_bytes(prefix, path)
        from .attempt_workspace import checked_path
        return checked_path(root, path).read_bytes()
    except OSError:
        return None


def _decode_json(data, default=None):
    if data is None:
        return default
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return default


def _json(prefix, path, default=None, *, root=None, load=None):
    return _decode_json(_read(prefix, path, root, load=load), default)


def _inline(value) -> str:
    if value is None:
        return "未配置"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip() or "未配置"


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(_inline(item) for item in row) + " |" for row in rows)
    return lines


def _mesh_inspection(quality, dynamic_refinement=None):
    rows = []
    preset = quality.get('quality_preset')
    if preset:
        rows.append(['质量预设', preset])
    families = quality.get('element_families')
    if isinstance(families, dict) and isinstance(families.get('native_counts'), dict):
        if 'geometric_dimension' in families:
            rows.append(['实际网格维度', families['geometric_dimension']])
        counts = '，'.join(f'{name} {count}' for name, count in sorted(families['native_counts'].items()))
        rows.append(['原生单元族及数量', counts])
    refinement = quality.get('refinement')
    if isinstance(refinement, dict) and {'axis','base_cells','refined_cells','selected_cells'} <= refinement.keys():
        rows.append(['方向加密', f"{refinement['axis']} 轴 · {refinement['base_cells']} → {refinement['refined_cells']} 单元"
                    f" · 选中 {refinement['selected_cells']} 单元"])
    if isinstance(dynamic_refinement,dict) and dynamic_refinement.get('changes'):
        changes=dynamic_refinement['changes']
        rows.append(['相界面动态加密',f"{dynamic_refinement['field']} · {changes[0]['before']} → {changes[-1]['after']} 单元"])
        rows.extend([f"网格变化（{change['time_s']} s）",f"{change['before']} → {change['after']} 单元"] for change in changes)
    layers = quality.get('boundary_layers')
    if (isinstance(layers, dict) and {'wall_faces','thickness_ranges_m','maximum_growth'} <= layers.keys()
            and isinstance(layers.get('plan'), dict) and 'patch' in layers['plan']):
        thicknesses = '，'.join(f'{low:.8g}' if low == high else f'{low:.8g}–{high:.8g}'
                                for low, high in layers['thickness_ranges_m'])
        rows.extend([
            ['边界层测量', f"{layers['plan']['patch']} · {layers['wall_faces']} 个壁面单元"],
            ['各层厚度范围（m）', thicknesses],
            ['实际最大增长率', f"{layers['maximum_growth']:.8g}"],
        ])
    return ['', *_table(['原生检查', '实测记录'], rows)] if rows else []


def _evidence_metrics(case, *, root=None, load=None):
    pointer = _json(case.s3_prefix, "D-post/data/latest-postprocess.json",root=root,load=load)
    if not isinstance(pointer, dict) or not pointer.get("job_id"):
        return None, None, None
    try:
        job_id = int(pointer["job_id"])
    except (ValueError, TypeError, OverflowError):
        return None, None, None
    directory = f"D-post/data/postprocess-{job_id}"
    raw = _read(case.s3_prefix, f"{directory}/metrics.json", root, load=load)
    metrics = _decode_json(raw)
    manifest = _json(case.s3_prefix, f"{directory}/artifact-manifest.json", root=root, load=load)
    return (metrics if isinstance(metrics, dict) else None,
            manifest if isinstance(manifest, dict) else None,
            (job_id, raw))


def _metric_source(case, dictionary, metrics, manifest, loaded):
    if loaded is None:
        return None
    selected_job, raw = loaded
    if (not metrics or not manifest or not raw
            or metrics.get("schema") != "vibeflow.postprocess.metrics.v1"
            or manifest.get("schema") != "vibeflow.postprocess.artifacts.v1"
            or type(metrics.get("job_id")) is not int or metrics["job_id"] != selected_job or selected_job <= 0
            or metrics["job_id"] != manifest.get("job_id")
            or type(metrics.get("case_id")) is not int or metrics["case_id"] <= 0
            or metrics.get("case_id") != manifest.get("case_id")
            or metrics.get("case_id") != getattr(case, "id", None)):
        return None
    source = metrics.get("source")
    readiness = metrics.get("readiness")
    if not isinstance(source, dict) or not isinstance(readiness, dict) or readiness.get("report_ready") is not True:
        return None
    if source.get("case_input_sha256"):
        if source["case_input_sha256"] != case_input_fingerprint(dictionary.decode("utf-8")):
            return None
    elif source.get("sha256") != hashlib.sha256(dictionary).hexdigest():
        return None
    path = f"D-post/data/postprocess-{metrics['job_id']}/metrics.json"
    digest = hashlib.sha256(raw).hexdigest()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    records = [row for row in artifacts
               if isinstance(row, dict) and row.get("path") == path]
    if len(records) != 1 or (records[0].get("size"), records[0].get("sha256")) != (len(raw), digest):
        return None
    return {"job_id": metrics["job_id"], "path": path, "sha256": digest,
            "dict_revision": source.get("dict_revision")}


def _finite_number(value):
    if type(value) not in (int, float, str):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _target_result(target, sources, by_name):
    result = {"status": "unverified"}
    if not isinstance(target, dict) or any(
            not isinstance(target.get(key), str) or not target[key].strip() for key in ("metric", "unit")):
        return {**result, "reason": "invalid_target"}
    name, unit = target["metric"], target["unit"]
    result.update(metric=name, unit=unit)
    if sources is None:
        return {**result, "reason": "evidence_unavailable"}
    candidates = by_name.get(name, [])
    if len(candidates) != 1:
        return {**result, "reason": "duplicate_metric" if candidates else "missing_metric"}
    metric = candidates[0]
    if metric.get("unit") != unit:
        return {**result, "reason": "unit_mismatch"}
    value = _finite_number(metric.get("value")) if type(metric.get("value")) in (int, float) else None
    if value is None:
        return {**result, "reason": "invalid_value"}
    return {**result, "status": "measured", "value": value}


def _criterion_result(criterion, targets):
    result = {"status": "unverified"}
    if not isinstance(criterion, dict) or not isinstance(criterion.get("target"), str) or not criterion["target"]:
        return {**result, "reason": "invalid_criterion"}
    result["target"] = criterion["target"]
    lower, upper = (_finite_number(criterion.get(bound)) for bound in ("min", "max"))
    if (("min" in criterion and lower is None) or ("max" in criterion and upper is None)
            or lower is None and upper is None or lower is not None and upper is not None and lower > upper):
        return {**result, "reason": "invalid_bounds"}
    result.update(min=lower, max=upper)
    target = targets.get(criterion["target"])
    if target is None:
        return {**result, "reason": "unknown_target"}
    if target["status"] != "measured":
        return {**result, "reason": target["reason"]}
    value = target["value"]
    passed = (lower is None or lower <= value) and (upper is None or value <= upper)
    return {**result, "status": "pass" if passed else "fail"}


def _intent_acceptance(case, intent, dictionary, metrics, manifest, raw, *, gate_reason):
    targets = intent.get("targetOutputs")
    criteria = intent.get("acceptanceCriteria")
    targets = targets if isinstance(targets, dict) else {}
    criteria = criteria if isinstance(criteria, dict) else {}
    if not (targets or criteria):
        return None
    evidence = None if gate_reason else _metric_source(case, dictionary, metrics, manifest, raw)
    by_name = {}
    if evidence is not None:
        rows = metrics.get("metrics")
        for metric in rows if isinstance(rows, list) else []:
            if isinstance(metric, dict) and isinstance(metric.get("name"), str):
                by_name.setdefault(metric["name"], []).append(metric)
    measured = {name: _target_result(target, evidence, by_name) for name, target in sorted(targets.items())}
    return {"evidence": evidence, "targets": measured, "criteria": {
        name: _criterion_result(criterion, measured) for name, criterion in sorted(criteria.items())
    }}


_ACCEPTANCE_LABELS = {
    "measured": "已取得指标", "pass": "符合", "fail": "超出范围", "unverified": "待核验",
    "invalid_target": "目标需要指标名和单位", "evidence_unavailable": "缺少当前指标证据",
    "duplicate_metric": "存在重名指标", "missing_metric": "未找到指标", "unit_mismatch": "单位不一致",
    "invalid_value": "指标值无效", "invalid_criterion": "验收标准需要目标引用",
    "invalid_bounds": "上下限无效", "unknown_target": "未找到目标",
}


def _acceptance_rows(result):
    def cell(value):
        return re.sub(r"([\\`*_\[\]~])", r"\\\1", escape(value, quote=False)) if isinstance(value, str) else value

    def status(row):
        return _ACCEPTANCE_LABELS[row.get("reason", row["status"])]

    def measured(row):
        value = row.get("value")
        if value is None:
            return "待核验"
        text = format(value, ".10g")
        display = text if float(text) == value else "≈" + text
        return f"{display} {cell(row['unit'])}"

    def bounds(row, target):
        lower, upper = row.get("min"), row.get("max")
        if lower is None and upper is None:
            return "待配置"
        unit = cell(target.get("unit")) or ""
        low = repr(lower).removesuffix(".0") if lower is not None else None
        high = repr(upper).removesuffix(".0") if upper is not None else None
        if low is None:
            return f"≤ {high} {unit}".strip()
        if high is None:
            return f"≥ {low} {unit}".strip()
        return f"{low}–{high} {unit}".strip()

    targets = result["targets"]
    lines = []
    if targets:
        lines.extend(_table(["目标输出", "指标", "实测值", "状态"], [
            [cell(name), cell(row.get("metric")), measured(row), status(row)]
            for name, row in targets.items()
        ]))
    if result["criteria"]:
        if lines:
            lines.append("")
        rows = []
        for name, row in result["criteria"].items():
            target_name = row.get("target")
            target = targets.get(target_name, {})
            label = f"{cell(name)} · {cell(target_name)}" if target_name else cell(name)
            rows.append([label, bounds(row, target), measured(target), status(row)])
        lines.extend(_table(["验收标准", "范围", "实测值", "判定"], rows))
    return lines


def _chart_links(chart: dict, directory: str, published_paths: set[str]) -> str:
    if not directory:
        return ""
    names = chart.get("artifacts")
    if not isinstance(names, list):
        legacy_path = chart.get("path")
        names = [legacy_path.removeprefix(f"{directory}/")] if isinstance(legacy_path, str) else []
    links = []
    for name in names:
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.(?:png|pdf|svg|plot\.csv)", name):
            path = f"{directory}/{name}"
            if path in published_paths:
                links.append(f"[{name}](/{path})")
    return ", ".join(links)


def _published_charts(metrics, manifest):
    job = (manifest or {}).get("job_id")
    directory = f"D-post/data/postprocess-{job}" if type(job) is int and job > 0 else ""
    records = {entry["path"]: entry for entry in ((manifest or {}).get("artifacts") or [])
               if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    charts = (metrics or {}).get("extractions", {}).get("charts") or []
    return directory, records, charts


def _figures(directory, records, charts):
    figures = []
    if not directory:
        return figures
    for chart in charts:
        identifier = chart.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            continue
        for suffix in ("png", "svg"):
            path = f"{directory}/{identifier}.{suffix}"
            record = records.get(path)
            names = chart.get("artifacts")
            if (record is None or not isinstance(record.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])
                    or (isinstance(names, list) and f"{identifier}.{suffix}" not in names)):
                continue
            figure = {"id": identifier, "path": path, "sha256": record["sha256"],
                      "title": chart.get("title") or identifier, "source": chart.get("source"),
                      "field": chart.get("field"), "unit": chart.get("y_unit")}
            data_path = f"{directory}/{identifier}.plot.csv"
            data = records.get(data_path)
            if (data and chart.get("data") == f"{identifier}.plot.csv"
                    and chart.get("layout") in ("overlay", "panels") and isinstance(chart.get("series"), list)
                    and isinstance(data.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", data["sha256"])):
                figure.update(layout=chart["layout"], series=chart["series"],
                              data_path=data_path, data_sha256=data["sha256"])
            figures.append(figure)
            break
    return figures


def _figure_markdown(figure, number):
    title = _inline(figure["title"])
    caption = re.sub(r"([\\`*_\[\]<>])", r"\\\1", f"图 {number} · {title}")
    link = quote(figure['path'],safe='/')
    return f"![{caption}](../{link}){{width=90%}}"


def _configured_figures(case, parsed, root, *, load=None):
    from .attempt_workspace import canonical_path, checked_path, digest_file

    report = parsed.get("report")
    figures = report.get("resultFigures", {}) if isinstance(report, dict) else {}
    if not isinstance(figures, dict) or len(figures) > 24:
        raise ValueError("report.resultFigures 必须是最多包含 24 项的图件字典")

    def case_file(value, directory, suffixes, label):
        try:
            if (not isinstance(value, str) or value != canonical_path(value)
                    or not value.startswith(directory+'/') or PurePosixPath(value).suffix.lower() not in suffixes):
                raise ValueError
        except ValueError as error:
            raise ValueError(f"{label} 必须是 {directory} 中的 {'、'.join(suffixes)} 文件") from error
        return value

    def digest(path):
        if root is not None:
            if load is not None and not load(path):
                raise ValueError("报告引用的算例文件不存在: " + path)
            try:
                return digest_file(checked_path(root,path))["sha256"]
            except FileNotFoundError as error:
                raise ValueError("报告引用的算例文件不存在: " + path) from error
        data = _read(case.s3_prefix,path,root)
        if data is None:
            raise ValueError("报告引用的算例文件不存在: " + path)
        return hashlib.sha256(data).hexdigest()

    result = []
    for identifier, spec in figures.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) or not isinstance(spec, dict) or set(spec)-{"image","title","data"}:
            raise ValueError("report.resultFigures 只接受有效图件 ID 和 image、title、data 字段")
        image = case_file(spec.get("image"),"D-post/figures",(".png",".jpg",".jpeg"),f"{identifier}.image")
        title = spec.get("title", identifier)
        if not isinstance(title, str) or not title.strip() or len(title) > 240:
            raise ValueError(f"{identifier}: title 须为 1 至 240 个字符")
        figure = {"id":identifier, "path":image, "title":title.strip(), "source":"caseFile", "sha256":digest(image)}
        if "data" in spec:
            data = case_file(spec["data"],"D-post/data",(".csv",),f"{identifier}.data")
            figure.update(data_path=data,data_sha256=digest(data))
        result.append(figure)
    return result


def published_figures(case, root, parsed, *, load=None):
    """Discover the same generated image references when sealing report inputs."""
    metrics, manifest, _ = _evidence_metrics(case, root=root, load=load)
    return _figures(*_published_charts(metrics, manifest)) + _configured_figures(case,parsed,root,load=load)


def compose_report(case, *, validated_gate=False, generated_at=None, root=None) -> dict:
    if root is None:data=storage.get_bytes(case.s3_prefix, CASE_DICT_PATH)
    else:
        from .attempt_workspace import checked_path
        data=checked_path(root,CASE_DICT_PATH).read_bytes()
    parsed = parse_casedict(data.decode("utf-8"))
    meta = parsed.get("caseMeta", {}) if isinstance(parsed.get("caseMeta"), dict) else {}
    intent = parsed.get("intent", {}) if isinstance(parsed.get("intent"), dict) else {}
    physical = parsed.get("physicalProblem", {}) if isinstance(parsed.get("physicalProblem"), dict) else {}
    report = parsed.get("report", {}) if isinstance(parsed.get("report"), dict) else {}
    template = profile(parsed)
    quality = _json(case.s3_prefix, "B-mesh/quality.json",root=root) or {}
    metrics, manifest, raw_metrics = _evidence_metrics(case,root=root)
    readiness = (metrics or {}).get("readiness", {})
    gate_reason = None if validated_gate else report_gate_reason(case)
    intent_acceptance = _intent_acceptance(case, intent, data, metrics, manifest, raw_metrics, gate_reason=gate_reason)
    title = report.get("title") or meta.get("title") or case.name
    chapters = []
    chapter_schema = []
    directory, records, charts = _published_charts(metrics, manifest)
    case_figures = _configured_figures(case,parsed,root)
    figures = _figures(directory, records, charts) + case_figures

    def heading(key):
        label, blocks, evidence = _CHAPTERS[key]
        if key == "results":
            label = template["results_title"]
        chapters.append(key)
        chapter_schema.append({"id": key, "title": label, "number": len(chapters),
                               "case_dict_blocks": list(blocks), "evidence": list(evidence),
                               "figures": figures if key == "results" else []})
        return f"## {len(chapters)}. {label} {{#{key}}}"

    lines = [
        f"# {_inline(title)}",
        "",
        "> 本文由 caseDict、网格质量和后处理证据自动组织；verification 与 validation 分开陈述。",
        "",
        heading("problem"),
        "",
        f"- 算例：`{_inline(case.name)}`；workflowState=`{_inline(case.workflow_state)}`。",
        f"- 学科/分析：`{_inline(meta.get('discipline'))}` / `{_inline(meta.get('analysisType'))}`；维度：`{_inline(meta.get('dimensions'))}`；fidelity：`{_inline(meta.get('fidelity'))}`。",
        f"- 问题类型：`{_inline(physical.get('problemClass'))}`；时间行为：`{_inline(physical.get('timeBehavior'))}`。",
        f"- 工程问题：`{_inline(intent.get('userIntent'))}`。",
        f"- 目标输出：`{_inline(intent.get('targetOutputs'))}`。",
        f"- 验收条件：`{_inline(intent.get('acceptanceCriteria'))}`。",
        f"- 未决问题：`{_inline(intent.get('openQuestions'))}`。",
        "",
        *(_acceptance_rows(intent_acceptance) + [""] if intent_acceptance else []),
        heading("assumptions"),
        "",
        f"- 假设：`{_inline(intent.get('assumptions', intent.get('primaryAssumptions')))}`。",
        "",
        heading("geometry"),
        "",
        f"- 几何参数：`{_inline(parsed.get('geometry', {}).get('parameters') if isinstance(parsed.get('geometry'), dict) else None)}`。",
        f"- 几何域：`{_inline(parsed.get('geometry', {}).get('domain') if isinstance(parsed.get('geometry'), dict) else None)}`。",
        f"- 几何脚本：[A-geo/geometry.py](../A-geo/geometry.py)。",
        f"- 材料：`{_inline(parsed.get('materials'))}`。",
        f"- 区域：`{_inline(parsed.get('regions'))}`。",
        f"- 界面/边界：`{_inline(parsed.get('interfaces'))}` / `{_inline(parsed.get('boundariesLoadsInitials'))}`。",
        "",
        heading("mesh"),
        "",
        f"- 网格参数：`{_inline(parsed.get('mesh'))}`。",
        f"- 网格质量状态：`{_inline(quality.get('status'))}`；policy=`{_inline(quality.get('policy'))}`；日志：`{_inline(quality.get('log_path'))}`。",
        f"- 质量指标：`{_inline(quality.get('metrics'))}`。",
        f"- 质量违规：`{_inline(quality.get('violations'))}`。",
        *_mesh_inspection(quality,((metrics or {}).get('source') or {}).get('dynamic_refinement')),
        "",
        heading("methods"),
        "",
        f"- 数值方法：`{_inline(parsed.get('numericalMethod'))}`。",
        f"- 执行配置：`{_inline(parsed.get('execution'))}`。",
        f"- 运行目录：`{_inline((metrics or {}).get('run_dir'))}`。",
        f"- 时间样本：`{_inline((metrics or {}).get('times'))}`。",
        "",
        heading("results"),
        "",
    ]
    if template["analysis_focus"]:
        lines.extend([template["analysis_focus"], ""])
    scalar_metrics = (metrics or {}).get("metrics") or []
    if scalar_metrics:
        lines.extend(_table(["指标", "操作", "时间", "值", "单位"], [
            [item.get("name"), item.get("operation"), item.get("time"), item.get("value"), item.get("unit")]
            for item in scalar_metrics
        ]))
    else:
        lines.append("尚未生成可引用的后处理指标；先运行 postprocess:run。")
    for number, figure in enumerate(figures, 1):
        lines.extend(["", _figure_markdown(figure, number)])
    lines.extend(["", heading("verification"), ""])
    lines.extend([
        f"- evidence_level：`{_inline(readiness.get('evidence_level'))}`；report_ready=`{_inline(readiness.get('report_ready'))}`。",
        f"- can_claim_verification=`{_inline(readiness.get('can_claim_verification'))}`；can_claim_validation=`{_inline(readiness.get('can_claim_validation'))}`。",
        "- 当前报告不把计算结果自动等同于实验 validation；需要独立实验数据与不确定度评估。",
        "",
    ])
    comparisons = (metrics or {}).get("comparisons") or []
    if comparisons:
        rows = []
        for item in comparisons:
            reference = item.get("reference") or {}
            status = item.get("status")
            tolerance = reference.get("l2_max", 0 if status in ("pass", "fail") else None)
            provenance = " · ".join(str(value) for value in (
                item.get("reference_id") or reference.get("id"), reference.get("kind")) if value)
            rows.append([item.get("metric"), provenance, status, item.get("l2_relative"), tolerance,
                         reference.get("source")])
        lines.extend(_table(["指标", "参考与类型", "状态", "相对 L2", "容差", "参考来源"], rows))
        for item in comparisons:
            alignment = item.get("alignment") or {}
            if alignment.get("kind") != "spatialProfile":
                continue
            lines.extend(["", f"- 空间剖面 `{_inline(item.get('metric'))}` 在求解时刻 "
                          f"`{_inline(alignment.get('time'))}` 对齐 `{alignment['samples']}` 个采样点，"
                          f"均方根误差 `{_inline(item.get('rmse'))}`。"])
            for label, details in (("测量剖面", alignment["reference"]), ("原始观测", alignment.get("source")),
                                   ("采集回执", alignment.get("receipt"))):
                if details:
                    lines.append(f"  - {label} `{_inline(details['path'])}`，SHA-256 `{_inline(details['sha256'])}`。")
    else:
        lines.append("当前没有可列出的参考场/参考值比较。")
    uncertainty = (metrics or {}).get("extractions", {}).get("uncertainty") or []
    lines.extend(["", heading("limitations"), ""])
    if uncertainty:
        lines.extend(_table(["项目", "扩展不确定度", "单位", "来源"], [
            [item.get("id") or item.get("name"), item.get("expanded"), item.get("unit"), item.get("source")]
            for item in uncertainty
        ]))
    else:
        lines.append("未配置独立的不确定度评估；不能据此推断结果误差范围。")
    errors = parsed.get("errors", {}) if isinstance(parsed.get("errors"), dict) else {}
    entries = errors.get("entries", {}) if isinstance(errors.get("entries"), dict) else {}
    open_errors = [item for item in entries.values() if isinstance(item, dict) and item.get("status") not in ("verified", "wontFix")]
    if open_errors:
        lines.extend(_table(["错误", "阶段", "状态", "说明"], [
            [item.get("title"), item.get("stage"), item.get("status"), item.get("message")]
            for item in open_errors
        ]))
    lines.extend([
        f"- 限制：`{_inline(intent.get('limitations', intent.get('knownLimitations')))}`。",
        f"- 未决问题：`{_inline(intent.get('openQuestions'))}`。",
        f"- 决策记录：`{_inline(intent.get('decisionLog'))}`。",
        f"- 报告门禁：`{_inline(gate_reason)}`。",
        "",
        heading("evidence"),
        "",
        f"- 统一参数源：[0-caseDict/caseDict](/0-caseDict/caseDict)（revision=`{_inline((metrics or {}).get('source', {}).get('dict_revision'))}`）。",
        "- 网格证据：[B-mesh/quality.json](/B-mesh/quality.json)。",
        "- 后处理指针：[D-post/data/latest-postprocess.json](/D-post/data/latest-postprocess.json)。",
        f"- 后处理配置 SHA-256：`{_inline((metrics or {}).get('source', {}).get('postprocess_sha256'))}`。",
        f"- caseDict SHA-256：`{_inline((metrics or {}).get('source', {}).get('sha256'))}`。",
        f"- 生成时间（UTC）：`{_inline((metrics or {}).get('generated_at') or generated_at or datetime.now(timezone.utc).isoformat())}`。",
        "",
    ])
    if charts:
        lines.extend(_table(["图件", "后端", "DPI", "文件"], [
            [item.get("id"), item.get("backend"), item.get("dpi"), _chart_links(item, directory, records.keys())]
            for item in charts
        ]))
    if case_figures:
        lines.extend(_table(["算例图件", "图件 SHA-256", "数据文件", "数据 SHA-256"], [
            [item["path"], item["sha256"], item.get("data_path"), item.get("data_sha256")] for item in case_figures
        ]))
    if directory:
        path = f"{directory}/artifact-manifest.json"
        lines.extend(["", f"证据清单：[{path}](/{path})。"])
    lines.extend(["", f"报告生成器：`report_builder.py`；章节 ID：`{', '.join(chapters)}`。"])
    evidence = {"metrics": bool(metrics), "manifest": bool(manifest),
                "report_ready": bool(readiness.get("report_ready", not gate_reason) and not gate_reason)}
    if intent_acceptance is not None:
        evidence["intent_acceptance"] = intent_acceptance
    return {"content": frontmatter(parsed) + "\n".join(lines) + "\n", "chapters": chapters,
            "chapter_schema": chapter_schema, "template": template, "chapter_schema_version": 1,
            "gate_reason": gate_reason, "evidence": evidence}
