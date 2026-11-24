"""网格质量报告与 workflow 门控：checkMesh 指标、policy、hash 和证据边界。"""
import copy
import json
import os
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from app.services.casedict import parse_casedict, set_block, set_workflow_state
from app.services.mesh_quality import mesh_gate_reason, parse_metrics

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


tok = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
assert tok
case = call("POST", "/workspaces/1/cases", {"name": f"mesh-quality-{time.time_ns()}"}, token=tok)
assert case.get("id"), case
case_id = case["id"]
before = call("GET", f"/cases/{case_id}/caseDict", token=tok)
base_targets = {"maxNonOrtho": 70, "maxSkewness": 0.4, "maxAspectRatio": 1.5, "minJacobian": 0.01, "policy": "fail"}
log_bad = """Mesh non-orthogonality Max: 85 average: 1
Max skewness = 0.5 OK.
Max aspect ratio = 2 OK.
Min Jacobian = 0.001
Mesh OK.
"""
log_good = """Mesh non-orthogonality Max: 65 average: 0.5
Max skewness = 0.1 OK.
Max aspect ratio = 1 OK.
Min Jacobian = 0.02
Mesh OK.
"""
call("PUT", f"/cases/{case_id}/file", {"path": "C-run/case-1/checkMesh.log", "content": log_bad}, token=tok)

# 真实 OpenFOAM 输出形式也能解析
metrics = parse_metrics("Mesh non-orthogonality Max: 0 average: 0\nMax skewness = 1.66533e-14 OK.\nMax aspect ratio = 1 OK.")
assert metrics["non_orthogonality"] == 0 and metrics["skewness"] > 0 and metrics["aspect_ratio"] == 1
print("1. checkMesh 实际格式指标解析 [OK]")


def set_targets(targets):
    current = call("GET", f"/cases/{case_id}/caseDict", token=tok)
    updated = set_block(current["content"], "mesh", {"source": "blockMesh", "qualityTargets": targets})
    call("PUT", f"/cases/{case_id}/caseDict", {"content": updated}, token=tok)


def evaluate():
    return call("POST", f"/cases/{case_id}/mesh:evaluate", {}, token=tok)


def gate():
    current = call("GET", f"/cases/{case_id}/caseDict", token=tok)
    mesh = parse_casedict(current["content"])["mesh"]
    targets = mesh["qualityTargets"]
    report = call("GET", f"/cases/{case_id}/file?path=B-mesh/quality.json", token=tok)
    with patch("app.services.mesh_quality.read_quality_report", return_value=json.loads(report["content"])):
        reason = mesh_gate_reason("isolated", mesh, targets["policy"], targets.get("reason"))
    before_rev = current["dict_revision"]
    result = {"_err": 409, "detail": reason} if reason else {"ok": True}
    after = call("GET", f"/cases/{case_id}/caseDict", token=tok)
    return result, before_rev, after


set_targets(base_targets)
report = evaluate()
assert report["status"] == "fail" and len(report["violations"]) == 4, report
assert report["metrics"]["non_orthogonality"] == 85 and report["case_dict_mesh_hash"]
result, before_rev, after = gate()
assert result.get("_err") == 409 and after["dict_revision"] == before_rev, (result, after)
print("2. fail 报告与 C 阶段阻断 [OK]")

# warn 允许推进但保留失败报告
set_targets(dict(base_targets, policy="warn")
            )
assert evaluate()["status"] == "fail"
result, _, _ = gate()
assert result.get("ok"), result
print("3. warn 保留失败报告，质量策略允许；真实阶段还需网格证据 [OK]")

# allowWithReason 无理由拒绝，有理由允许
set_targets(dict(base_targets, policy="allowWithReason")
            )
assert evaluate()["status"] == "fail"
result, before_rev, after = gate()
assert result.get("_err") == 409 and after["dict_revision"] == before_rev, (result, after)
set_targets(dict(base_targets, policy="allowWithReason", reason="benchmark accepts degraded mesh")
            )
assert evaluate()["status"] == "fail"
result, _, _ = gate()
assert result.get("ok"), result
print("4. allowWithReason 的理由门控 [OK]")

# 合格报告允许 fail 策略通过
call("PUT", f"/cases/{case_id}/file", {"path": "C-run/case-1/checkMesh.log", "content": log_good}, token=tok)
set_targets(base_targets)
assert evaluate()["status"] == "pass"
result, _, _ = gate()
assert result.get("ok"), result
print("5. 合格报告通过 [OK]")

# 改 mesh 参数后旧 hash 报告不能继续使用
set_targets(dict(base_targets, maxNonOrtho=60)
            )
result, before_rev, after = gate()
assert result.get("_err") == 409 and after["dict_revision"] == before_rev, (result, after)
print("6. mesh 参数变脏后旧报告阻断 [OK]")

# 缺报告和缺指标都 fail closed
missing_case = call("POST", "/workspaces/1/cases", {"name": f"mesh-missing-{time.time_ns()}"}, token=tok)
missing_id = missing_case["id"]
current = call("GET", f"/cases/{missing_id}/caseDict", token=tok)
updated = set_block(current["content"], "mesh", {"source": "blockMesh", "qualityTargets": base_targets})
call("PUT", f"/cases/{missing_id}/caseDict", {"content": updated}, token=tok)
gated = set_workflow_state(updated, "C")
missing_result = call("PUT", f"/cases/{missing_id}/caseDict", {"content": gated}, token=tok)
assert missing_result.get("_err") == 409, missing_result
print("7. 缺质量报告 fail closed [OK]")

# 缺 Jacobian 指标视为未证明质量，不允许通过
set_targets(base_targets)
call("PUT", f"/cases/{case_id}/file", {"path": "C-run/case-1/checkMesh.log", "content": "Mesh non-orthogonality Max: 0 average: 0\nMesh OK.\n"}, token=tok)
report = evaluate()
assert report["status"] == "fail" and any(v["reason"] == "missing_metric" for v in report["violations"]), report
print("8. 缺指标按未证明处理 [OK]")

# A warning/waiver cannot turn missing measurements into observed evidence.
for policy in ('warn', 'allowWithReason'):
    set_targets(dict(base_targets,policy=policy,reason='accept measured degradation'))
    assert evaluate()['status']=='fail'
    rejected,_,_=gate()
    assert rejected.get('_err')==409,rejected
print('8b. warn/allowWithReason 不能放行缺失指标 [OK]')

# 非法 policy 在写文件前拒绝
invalid = set_block(before["content"], "mesh", {"source": "blockMesh", "qualityTargets": dict(base_targets, policy="sometimes")})
invalid_result = call("PUT", f"/cases/{case_id}/caseDict", {"content": invalid}, token=tok)
assert invalid_result.get("_err") == 422, invalid_result
print("9. 非法 qualityTargets.policy 422 [OK]")

# 日志路径越界拒绝
traversal = call("POST", f"/cases/{case_id}/mesh:evaluate", {"log_path": "../secret.log"}, token=tok)
assert traversal.get("_err") == 400, traversal
print("10. 日志路径越界 400 [OK]")

print("\n=== 网格质量报告与门控冒烟通过 ===")
