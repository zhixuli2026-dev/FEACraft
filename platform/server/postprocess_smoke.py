"""WP06 后处理垂直切片：真实 OpenFOAM 场 → VTK 提取 → 指标/证据/readiness。

使用一单元制造解，独立期望值不来自被测输出：
- p 的四个时刻为 2/3/2/1，U 恒为 (1,0,0)，立方体体积 1、外表面面积 6。
- 频谱对 0.1 s 等间距采样，去均值后的主频应为 2.5 Hz。
"""
import csv
import io
import json
import math
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from app.services.casedict import set_block
from app.services.postprocess_executor import (PostprocessError, _json_bytes, _safe_rel, build_comparisons,
                                               build_readiness, merge_artifacts, validate_metric_names)

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, timeout=180):
    for _ in range(timeout):
        state = call("GET", f"/jobs/{job_id}", token=tok)
        if state.get("status") not in ("queued", "running"):
            return state
        time.sleep(1)
    raise TimeoutError(f"job {job_id}")


def put(path, content):
    result = call("PUT", f"/cases/{case_id}/file", {"path": path, "content": content}, token=tok)
    assert result.get("ok"), (path, result)


def raw(path):
    req = urllib.request.Request(B + f"/cases/{case_id}/raw?path={urllib.parse.quote(path, safe='')}")
    req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req) as response:
        return response.read()


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"postprocess-{time.time_ns()}"}, token=tok)
assert case.get("id"), case
case_id = case["id"]
H = "FoamFile\n{\n version 2.0;\n format ascii;\n class %s;\n object %s;\n}\n\n"
files = {
    "C-run/case-1/system/blockMeshDict": H % ("dictionary", "blockMeshDict") + """
convertToMeters 1;
vertices ((0 0 0) (1 0 0) (1 1 0) (0 1 0) (0 0 1) (1 0 1) (1 1 1) (0 1 1));
blocks (hex (0 1 2 3 4 5 6 7) (1 1 1) simpleGrading (1 1 1));
boundary
(
    xMin { type wall; faces ((0 4 7 3)); }
    xMax { type wall; faces ((1 2 6 5)); }
    yMin { type wall; faces ((0 1 5 4)); }
    yMax { type wall; faces ((3 7 6 2)); }
    zMin { type wall; faces ((0 3 2 1)); }
    zMax { type wall; faces ((4 5 6 7)); }
);
""",
    "C-run/case-1/system/controlDict": H % ("dictionary", "controlDict") + """
application icoFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime 0.3;
deltaT 0.1;
writeControl timeStep;
writeInterval 1;
writeFormat ascii;
""",
    "C-run/case-1/system/fvSchemes": H % ("dictionary", "fvSchemes"),
    "C-run/case-1/system/fvSolution": H % ("dictionary", "fvSolution"),
}
for path, content in files.items():
    put(path, content)

for time_name, p in zip(("0", "0.1", "0.2", "0.3"), (2, 3, 2, 1)):
    put(f"C-run/case-1/{time_name}/p", H % ("volScalarField", "p") + f"""
dimensions [0 0 0 0 0 0 0];
internalField uniform {p};
boundaryField {{ xMin {{ type zeroGradient; }} xMax {{ type zeroGradient; }}
    yMin {{ type zeroGradient; }} yMax {{ type zeroGradient; }}
    zMin {{ type zeroGradient; }} zMax {{ type zeroGradient; }} }}
""")
    put(f"C-run/case-1/{time_name}/U", H % ("volVectorField", "U") + """
dimensions [0 1 -1 0 0 0 0];
internalField uniform (1 0 0);
boundaryField { xMin { type fixedValue; value uniform (1 0 0); }
    xMax { type fixedValue; value uniform (1 0 0); }
    yMin { type fixedValue; value uniform (1 0 0); }
    yMax { type fixedValue; value uniform (1 0 0); }
    zMin { type fixedValue; value uniform (1 0 0); }
    zMax { type fixedValue; value uniform (1 0 0); } }
""")

mesh_job = call("POST", f"/cases/{case_id}/jobs", {
    "type": "mesh", "runtime": "wsl",
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && blockMesh",
}, token=tok)
mesh_state = wait_job(mesh_job["id"])
assert mesh_state["status"] == "succeeded", mesh_state
print("1. 独立一单元 OpenFOAM 制造解已生成 [OK]")

def by_id(items):
    return {item["id"]: {key: value for key, value in item.items() if key != "id"} for item in items}


postprocessing = {
    "objectives": {
        "comparisonTargets": {
            "formulaBenchmark": {
                "metric": "p_times_two.value", "reference": "formulaCheck", "type": "benchmark",
            },
        },
    },
    "runDir": "C-run/case-1",
    "fields": {"p": {"unit": "1"}, "U": {"unit": "m/s"}},
    "dataExtraction": {
        "pointProbes": by_id([{
            "id": "probes", "fields": ["p", "U"],
            "points": [0.5, 0.5, 0.5, 0.25, 0.25, 0.25],
        }]),
        "lineSamples": by_id([{
            "id": "centerline", "fields": ["p"], "start": [0, 0.5, 0.5],
            "end": [1, 0.5, 0.5], "samples": 5, "time": 0.3,
        }]),
        "surfaceIntegrals": by_id([{"id": "boundary_p", "field": "p", "time": 0.3}]),
        "volumeAverages": by_id([
            {"id": "volume_p", "field": "p", "time": 0.3},
            {"id": "volume_U", "field": "U", "time": 0.3},
        ]),
        "frequencySpectra": by_id([{"id": "p_spectrum", "source": "probes", "field": "p"}]),
        "modalQuantities": by_id([{"id": "p_pod", "source": "probes", "field": "p", "rank": 2, "detrend": True}]),
    },
    "derivedQuantities": {
        "referenceFields": {
            "p_reference": {
                "field": "p", "path": "Ref/p-verification", "time": 0.3,
                "kind": "verification", "source": "manufactured uniform reference field", "l2_max": 1e-12,
            },
        },
        "formulas": {
            "p_times_two": {
                "expression": "volume_p * 2",
                "inputs": {"volume_p": '"volume_p.p@0.3"'},
                "unit": "1",
            },
        },
        "uncertainty": {
            "p_formula_uncertainty": {
                "metric": "p_times_two.value", "standard": 0.01, "coverage": 2,
                "source": "manufactured standard uncertainty",
            },
        },
        "referenceValues": {
            "volumeAt03": {
                "metric": '"volume_p.p@0.3"', "value": 1, "unit": "1",
                "kind": "verification", "l2_max": 1e-12,
                "source": "manufactured uniform p(t=0.3)=1",
            },
            "boundaryAt03": {
                "metric": '"boundary_p.p@0.3"', "value": 6, "unit": '"m^2"',
                "kind": "verification", "l2_max": 1e-12,
                "source": "unit cube external area 6 times p=1",
            },
            "formulaCheck": {
                "metric": '"p_times_two.value"', "value": 2, "unit": "1",
                "kind": "verification", "l2_max": 1e-12,
                "source": "manufactured formula 2 times volume average",
            },
        },
    },
    "artifacts": {
        "plots": {
            "probe_p": {
                "source": "probes", "field": "p", "component": 0, "probe": 0,
                "title": "Manufactured probe pressure", "xLabel": "Time", "yLabel": "Pressure",
                "formats": ["pdf", "svg", "png"], "dpi": 300,
            },
            "centerline_p": {
                "source": "centerline", "field": "p", "component": 0, "time": 0.3,
                "title": "Manufactured centerline pressure", "xLabel": "Distance", "yLabel": "Pressure",
                "formats": ["svg"], "dpi": 300,
            },
        },
    },
}
def save_postprocessing(value):
    current = call("GET", f"/cases/{case_id}/caseDict", token=tok)
    updated = set_block(current["content"], "postProcessing", value)
    result = call("PUT", f"/cases/{case_id}/caseDict", {"content": updated}, token=tok)
    assert result.get("ok"), result


REFERENCE_FIELD = H % ("volScalarField", "p") + """
dimensions [0 0 0 0 0 0 0];
internalField uniform 1;
boundaryField { }
"""
put("Ref/p-verification", REFERENCE_FIELD)
save_postprocessing(postprocessing)
run = call("POST", f"/cases/{case_id}/postprocess:run", {}, token=tok)
assert run.get("job_id"), run
job_id = run["job_id"]
state = wait_job(job_id)
assert state["status"] == "succeeded", state
result = call("GET", f"/cases/{case_id}/postprocess/results", token=tok)
assert result.get("job_id") == job_id, result
metrics_doc = result["metrics"]
manifest = result["manifest"]
assert metrics_doc["schema"] == "vibeflow.postprocess.metrics.v1"
assert manifest["config_sha256"] and manifest["vtk_version"]
assert manifest["source"]["path"] == "0-caseDict/caseDict"
assert manifest["source"]["sha256"] and manifest["source"]["dict_revision"] > 0
assert manifest["source"]["postprocess_sha256"]
assert any(a["path"].endswith("run-config.json") for a in manifest["artifacts"])
assert any(a["path"].endswith("metrics.json") for a in manifest["artifacts"])
assert any(a["path"].endswith("probes.csv") for a in manifest["artifacts"])
assert any(i["path"] == "Ref/p-verification" and i["sha256"] for i in manifest["inputs"])
print("2. 专用作业、版本化 metrics 与 artifact manifest [OK]")

probes = metrics_doc["extractions"]["pointProbes"][0]
assert probes["times"] == [0, 0.1, 0.2, 0.3], probes["times"]
assert probes["series"]["p"]["values"] == [[2, 2], [3, 3], [2, 2], [1, 1]], probes["series"]["p"]
assert probes["series"]["U"]["values"][0][0] == [1, 0, 0]
line = metrics_doc["extractions"]["lineSamples"][0]
assert line["series"]["p"]["values"] == [[1], [1], [1], [1], [1]], line
scalar_metrics = {m["name"]: m["value"] for m in metrics_doc["metrics"]}
assert math.isclose(scalar_metrics["volume_p.p@0.3"], 1, abs_tol=1e-12, rel_tol=0)
assert math.isclose(scalar_metrics["volume_U.U[0]@0.3"], 1, abs_tol=1e-12, rel_tol=0)
assert math.isclose(scalar_metrics["boundary_p.p@0.3"], 6, abs_tol=1e-12, rel_tol=0), scalar_metrics
assert math.isclose(scalar_metrics["p_times_two.value"], 2, abs_tol=1e-12, rel_tol=0), scalar_metrics
uncertainty = metrics_doc["extractions"]["uncertainty"][0]
assert uncertainty["status"] == "pass" and uncertainty["expanded"] == 0.02, uncertainty
assert uncertainty["unit"] == "1" and uncertainty["source"]
print("3. 点探针、线采样、面积分和体平均独立数值断言 [OK]")

spectrum = metrics_doc["extractions"]["frequencySpectra"][0]
assert math.isclose(spectrum["dominant_frequency_hz"], 2.5, abs_tol=1e-12, rel_tol=0), spectrum
assert math.isclose(spectrum["dominant_amplitude"], 1, abs_tol=1e-12, rel_tol=0), spectrum
pod = metrics_doc["extractions"]["modalQuantities"][0]
assert math.isclose(pod["energy_fractions"][0], 1, abs_tol=1e-12, rel_tol=0), pod
assert pod["method"] == "POD-SVD"
print("4. 频率谱与时间 POD/SVD [OK]")

comparisons = {c["metric"]: c for c in metrics_doc["comparisons"]}
assert comparisons["volume_p.p@0.3"]["l2_relative"] == 0
assert comparisons["boundary_p.p@0.3"]["status"] == "pass"
assert comparisons["p_times_two.value"]["status"] == "pass"
assert comparisons["p_reference.p@0.3"]["status"] == "pass"
assert comparisons["p_reference.p@0.3"]["l2_relative"] == 0
assert comparisons["p_reference.p@0.3"]["reference"]["path"] == "Ref/p-verification"
targets = metrics_doc["extractions"]["comparisonTargets"]
assert len(targets) == 1 and targets[0]["status"] == "pass", targets
assert targets[0]["type"] == "benchmark" and targets[0]["evidence_kind"] == "verification"
assert targets[0]["reference_id"] == "formulaCheck"
assert all(c["reference"]["source"] for c in metrics_doc["comparisons"])
readiness = metrics_doc["readiness"]
assert readiness["report_ready"] and readiness["can_claim_verification"], readiness
assert not readiness["can_claim_validation"], readiness
print("5. 参考来源、L2 与 verification/validation 证据边界 [OK]")

probes_csv = call("GET", f"/cases/{case_id}/file?path=D-post/data/postprocess-{job_id}/probes.csv", token=tok)
rows = list(csv.DictReader(io.StringIO(probes_csv["content"])))
assert rows and {row["field"] for row in rows} == {"p", "U"}
p_rows = [row for row in rows if row["field"] == "p" and row["component"] == "0"]
independent_mean = sum(float(row["value"]) for row in p_rows) / len(p_rows)
assert math.isclose(independent_mean, 2, abs_tol=1e-12, rel_tol=0), independent_mean
print("6. CSV 可独立重算（8 个 p 样本均值=2） [OK]")

charts = metrics_doc["extractions"]["charts"]
assert len(charts) == 2, charts
chart = next(item for item in charts if item["id"] == "probe_p")
assert chart["backend"] == "Agg" and chart["dpi"] == 300, chart
assert chart["color"].lower() == "#0072b2"
assert chart["provenance"]["case_dict_sha256"] == manifest["source"]["sha256"]
artifact_paths = {artifact["path"] for artifact in manifest["artifacts"]}
for extension in ("pdf", "svg", "png"):
    assert f"D-post/data/postprocess-{job_id}/probe_p.{extension}" in artifact_paths
pdf = raw(f"D-post/data/postprocess-{job_id}/probe_p.pdf")
assert pdf.startswith(b"%PDF") and len(pdf) > 1000
svg = raw(f"D-post/data/postprocess-{job_id}/probe_p.svg").decode("utf-8")
assert "Time [s]" in svg and "Pressure [1]" in svg
assert "#0072b2" in svg.lower() and f"case={case_id}" in svg and f"job={job_id}" in svg
assert manifest["source"]["sha256"][:12] in svg
png = raw(f"D-post/data/postprocess-{job_id}/probe_p.png")
assert png.startswith(b"\x89PNG\r\n\x1a\n")
assert struct.unpack(">II", png[16:24]) == (1800, 1200), struct.unpack(">II", png[16:24])
line_chart = next(item for item in charts if item["id"] == "centerline_p")
assert line_chart["x_unit"] == "m" and line_chart["time"] == 0.3, line_chart
assert f"D-post/data/postprocess-{job_id}/centerline_p.svg" in artifact_paths
line_svg = raw(f"D-post/data/postprocess-{job_id}/centerline_p.svg").decode("utf-8")
assert "Distance [m]" in line_svg and "Pressure [1]" in line_svg
print("7. Agg 出版图 PDF/SVG/300dpi PNG、单位、配色和来源追溯 [OK]")

# 新作业失败不得覆盖最近一次有效证据。
bad_post = json.loads(json.dumps(postprocessing))
bad_post["dataExtraction"]["pointProbes"] = {
    "outside": {"fields": ["p"], "points": [2, 2, 2]},
}
save_postprocessing(bad_post)
bad_run = call("POST", f"/cases/{case_id}/postprocess:run", {}, token=tok)
bad_state = wait_job(bad_run["job_id"])
assert bad_state["status"] == "failed", bad_state
latest = call("GET", f"/cases/{case_id}/postprocess/results", token=tok)
assert latest["job_id"] == job_id, latest
print("8. 越界探针失败且保留上一有效证据 [OK]")

# 报告门禁验证工程输入/场/产物；无关 revision 变化不应造成永久过期。
save_postprocessing(postprocessing)
put("E-report/report.md", "# Manufactured evidence\n")
put("E-report/latex/main.tex", "\\documentclass{article}\\begin{document}Evidence\\end{document}\n")
initial_report = call("POST", f"/cases/{case_id}/report:build", {}, token=tok)
assert initial_report.get("job_id"), initial_report
assert wait_job(initial_report["job_id"])["status"] == "succeeded"
put("Ref/p-verification", REFERENCE_FIELD.replace("internalField uniform 1", "internalField uniform 2"))
before_reference_change = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
changed_reference_report = call("POST", f"/cases/{case_id}/report:build", {}, token=tok)
after_reference_change = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
assert changed_reference_report.get("_err") == 409, changed_reference_report
assert after_reference_change == before_reference_change
put("Ref/p-verification", REFERENCE_FIELD)
stale_post = json.loads(json.dumps(postprocessing))
stale_post["dataExtraction"]["volumeAverages"]["volume_p"]["time"] = 0.2
save_postprocessing(stale_post)
before_stale = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
stale_report = call("POST", f"/cases/{case_id}/report:build", {}, token=tok)
stale_latex = call("POST", f"/cases/{case_id}/latex:build", {}, token=tok)
after_stale = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
assert stale_report.get("_err") == 409 and stale_latex.get("_err") == 409, (stale_report, stale_latex)
assert after_stale == before_stale
save_postprocessing(postprocessing)
restored_report = call("POST", f"/cases/{case_id}/report:build", {}, token=tok)
assert restored_report.get("job_id"), restored_report
assert wait_job(restored_report["job_id"])["status"] == "succeeded"
print("9. 报告/LaTeX 拒绝过期证据，恢复同一 postProcessing 后允许构建 [OK]")

not_ready_post = json.loads(json.dumps(postprocessing))
del not_ready_post["fields"]["p"]["unit"]
not_ready_post["artifacts"]["plots"] = {}
save_postprocessing(not_ready_post)
not_ready_run = call("POST", f"/cases/{case_id}/postprocess:run", {}, token=tok)
not_ready_state = wait_job(not_ready_run["job_id"])
assert not_ready_state["status"] == "succeeded", not_ready_state
not_ready_result = call("GET", f"/cases/{case_id}/postprocess/results", token=tok)
assert not_ready_result["metrics"]["readiness"]["report_ready"] is False
before_blocked = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
blocked_report = call("POST", f"/cases/{case_id}/report:build", {}, token=tok)
blocked_latex = call("POST", f"/cases/{case_id}/latex:build", {}, token=tok)
after_blocked = {item["id"] for item in call("GET", f"/cases/{case_id}/jobs", token=tok)}
assert blocked_report.get("_err") == 409 and blocked_latex.get("_err") == 409, (blocked_report, blocked_latex)
assert after_blocked == before_blocked
print("10. report_ready=false 在创建报告/LaTeX 作业前 409 [OK]")

# 恢复有效配置和最新证据，避免把测试case留在不可用状态。
save_postprocessing(postprocessing)
restore_run = call("POST", f"/cases/{case_id}/postprocess:run", {}, token=tok)
assert wait_job(restore_run["job_id"])["status"] == "succeeded"

for bad_dir in ("../secret", r"D:\vibeflow_reverse\platform\server\data\files\workspaces\1\cases\1", "//server/share"):
    traversal = call("POST", f"/cases/{case_id}/postprocess:run", {"run_dir": bad_dir}, token=tok)
    assert traversal.get("_err") == 400, (bad_dir, traversal)
    try:
        _safe_rel(bad_dir)
        raise AssertionError(f"执行器层未拒绝路径: {bad_dir}")
    except PostprocessError:
        pass
print("11. 相对越界、Windows 盘符和 UNC 路径均在作业前 400 [OK]")

# 审查回归：产物/指标不能冲突，坏参考值不能使 readiness 崩溃。
for sources in (
    ({"metrics.json": b"real"}, {"metrics.json": b"collision"}),
    ({"probes.csv": b"real"}, {"probes.csv": b"collision"}),
    ({"artifact-manifest.json": b"collision"},),
):
    try:
        merge_artifacts(*sources)
        raise AssertionError(f"产物冲突未拒绝: {sources}")
    except PostprocessError:
        pass
try:
    validate_metric_names([{"name": "x.p@0.3"}, {"name": "x.p@0.3"}])
    raise AssertionError("重复指标名未拒绝")
except PostprocessError:
    pass
print("12. metrics/probes/manifest 路径和重复指标名均 fail closed [OK]")

metric = {"name": "volume_p.p@0.3", "value": 1, "unit": "1"}
malformed = build_comparisons([metric], {"volume_p.p@0.3": 1})
assert malformed[0]["status"] == "invalid_reference", malformed
malformed_readiness = build_readiness({}, set(), malformed, 1)
assert not malformed_readiness["report_ready"], malformed_readiness
zero_reference = build_comparisons([metric], {"volume_p.p@0.3": {
    "value": 0, "unit": "1", "kind": "verification", "source": "manufactured zero", "l2_max": 1e-12,
}})
assert zero_reference[0]["status"] == "fail" and zero_reference[0]["l2_relative"] is None, zero_reference
assert b'"l2_relative": null' in _json_bytes({"comparisons": zero_reference})
print("13. 非字典参考值和零参考值均产生 JSON 安全的失败证据 [OK]")

from app.services.postprocess_executor import evaluate_formula
assert evaluate_formula("sqrt(volume_p)", {"volume_p": 4}) == 2
try:
    evaluate_formula("open('D-post/formula-should-not-exist.txt', 'w').write('x')", {})
    raise AssertionError("公式沙箱允许任意 open 调用")
except PostprocessError:
    pass
forbidden_file = call("GET", f"/cases/{case_id}/file?path=D-post/formula-should-not-exist.txt", token=tok)
assert forbidden_file.get("_err") == 404, forbidden_file
print("14. AST 公式白名单可用，任意函数调用不产生文件 [OK]")

from app.services.postprocess_executor import evaluate_uncertainty
invalid_uncertainty = evaluate_uncertainty([{
    "id": "missing_source", "metric": "volume_p.p@0.3", "standard": 0.1, "source": "",
}], [metric])
assert invalid_uncertainty[0]["status"] == "invalid", invalid_uncertainty
uncertainty_readiness = build_readiness({}, set(), [], 1, [metric], invalid_uncertainty)
assert not uncertainty_readiness["report_ready"], uncertainty_readiness
print("15. 缺来源不确定度进入 readiness blockers [OK]")

from app.services.postprocess_executor import apply_comparison_targets
bad_targets = apply_comparison_targets(metrics_doc["comparisons"], [{
    "id": "wrong_evidence", "metric": "p_times_two.value", "reference": "formulaCheck", "type": "experiment",
}])
assert bad_targets[0]["status"] == "kind_mismatch", bad_targets
target_readiness = build_readiness({}, set(), [], 1, [], [], bad_targets)
assert not target_readiness["report_ready"], target_readiness
validation_target = apply_comparison_targets([{
    "metric": "experiment_metric", "reference_id": "experimentRef",
    "reference": {"kind": "validation"}, "status": "pass",
}], [{"id": "experiment_ok", "metric": "experiment_metric", "reference": "experimentRef", "type": "experiment"}])
assert validation_target[0]["status"] == "pass", validation_target
for target_type in ("previousRun", "requirement"):
    regression_target = apply_comparison_targets([{
        "metric": "regression_metric", "reference_id": "regressionRef",
        "reference": {"kind": "regression"}, "status": "pass",
    }], [{"id": "regression_ok", "metric": "regression_metric", "reference": "regressionRef", "type": target_type}])
    assert regression_target[0]["status"] == "pass", (target_type, regression_target)
print("16. comparisonTargets 四类映射与类型错配 readiness [OK]")

print("\n=== WP06 后处理指标与数值证据垂直切片通过 ===")
