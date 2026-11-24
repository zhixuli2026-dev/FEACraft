"""端到端冒烟测试：register → workspace → case(脚手架入S3) → tree → 读写文件 → caseDict 状态推进"""
import json
import os
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")

def call(method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(B + path, method=method)
    if token: req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data) as r:
        payload = r.read()
        return payload if raw else json.loads(payload)

# 1. 注册（幂等：已存在则登录）
import urllib.error
try:
    r = call("POST", "/auth/register", {"email": "dev@vibeflow.cn", "name": "dev", "password": "devpass123"})
    print("1. register ok, user:", r["user"]["email"])
except urllib.error.HTTPError as e:
    if e.code != 409: raise
    r = {"token": call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]}
    print("1. login ok（已注册过）")
tok = r["token"]

# 2. 工作区
ws = call("GET", "/workspaces", token=tok)
print("2. workspaces:", [w["name"] for w in ws])
ws_id = ws[0]["id"]

# 3. 建 case（写脚手架到 S3）
existing = call("GET", f"/workspaces/{ws_id}/cases", token=tok)
dup = next((x for x in existing if x["name"] == "cavity-demo"), None)
if dup:
    case_id = dup["id"]
    print("3. case exists:", dup["id"])
else:
    c = call("POST", f"/workspaces/{ws_id}/cases", {"name": "cavity-demo"}, token=tok)
    case_id = c["id"]
    print("3. case created:", c)

# 4. 文件树
tree = call("GET", f"/cases/{case_id}/tree", token=tok)
paths = [e["path"] for e in tree["entries"]]
print(f"4. tree: {len(paths)} 项")
for p in sorted(paths)[:14]: print("   ", p)
assert "0-caseDict/caseDict" in paths and "A-geo/geometry.py" in paths

# 5. 读写文件
w = call("PUT", f"/cases/{case_id}/file", {"path": "D-post/notes.md", "content": "# 试算笔记\nhello s3"}, token=tok)
rd = call("GET", f"/cases/{case_id}/file?path=D-post/notes.md", token=tok)
assert rd["content"].startswith("# 试算笔记")
print("5. write+read file ok:", rd["path"], rd["size"], "bytes")

# 6. caseDict 读取与状态推进
cd = call("GET", f"/cases/{case_id}/caseDict", token=tok)
print("6. caseDict: state =", cd["workflow_state"], "| parse =", cd["parse_status"], "| keys:", list(cd["parsed"].keys())[:6])
rev0 = cd["dict_revision"]
try:
    call("PUT", f"/cases/{case_id}/caseDict",
         {"content": cd["content"], "workflow_state": "B"}, token=tok)
    raise AssertionError("普通写入不得推进阶段")
except urllib.error.HTTPError as error:
    assert error.code == 409
assert call("GET", f"/cases/{case_id}/caseDict", token=tok)["dict_revision"] == rev0
print("   raw state bypass rejected [OK]")

# 7. 非法 caseDict 应被拒
try:
    call("PUT", f"/cases/{case_id}/caseDict", {"content": "broken { {"}, token=tok)
    print("7. ERROR: 非法内容被接受了!")
except Exception as e:
    print("7. 非法 caseDict 被正确拒绝:", str(e)[:60])

# 7b. schema 枚举/结构错误必须 422，且文件/revision 不变；合法枚举可保存并还原
before_schema = call("GET", f"/cases/{case_id}/caseDict", token=tok)


def reject_schema(label, content):
    before = call("GET", f"/cases/{case_id}/caseDict", token=tok)
    try:
        result = call("PUT", f"/cases/{case_id}/caseDict", {"content": content}, token=tok)
        raise AssertionError(f"{label} 被接受: {result}")
    except urllib.error.HTTPError as e:
        assert e.code == 422, (label, e.code)
        after = call("GET", f"/cases/{case_id}/caseDict", token=tok)
        assert after["content"] == before["content"], f"{label} 改写了文件"
        assert after["dict_revision"] == before["dict_revision"], f"{label} 改写了 revision"
        print(f"7b. {label} 422 且文件/revision 不变 [OK]")
    finally:
        call("PUT", f"/cases/{case_id}/caseDict", {"content": before["content"]}, token=tok)


bad_status = before_schema["content"].replace("caseMeta\n{", "caseMeta\n{\n    status unknown;", 1)
reject_schema("caseMeta.status 非法", bad_status)
bad_time = before_schema["content"].replace("physicalProblem\n{", "physicalProblem\n{\n    timeBehavior sometimes;", 1)
reject_schema("timeBehavior 非法", bad_time)
bad_summary = before_schema["content"].replace("errors\n{", "errors\n{\n    summary { open -1; }", 1)
reject_schema("errors.open 负数", bad_summary)
bad_mapping = before_schema["content"].replace("caseMeta\n{", "caseMeta\n{\n    status readyForRun;", 1)
reject_schema("status/workflowState 不匹配", bad_mapping)
valid_schema = before_schema["content"].replace(
    "caseMeta\n{",
    "caseMeta\n{\n    status readyForGeometry;\n    discipline fluid;\n    analysisType steady;\n    dimensions 3D;\n    fidelity smoke;\n    executionProfile local;\n    units { length m; time s; }",
    1)
try:
    accepted = call("PUT", f"/cases/{case_id}/caseDict", {"content": valid_schema}, token=tok)
    assert accepted["ok"]
    print("7b. 合法 caseMeta/units 枚举可保存 [OK]")
finally:
    call("PUT", f"/cases/{case_id}/caseDict", {"content": before_schema["content"]}, token=tok)

# 7c. 全部 status 与 workflowState 映射逐项成立
from app.services.casedict import parse_casedict, set_block, set_workflow_state
from app.services.casedict_schema import validate_case_dict_data
for status, state in {
    "draft": "A", "readyForGeometry": "A", "readyForMesh": "B", "readyForRun": "C",
    "readyForPost": "D", "readyForReport": "E", "completed": "E",
}.items():
    mapped = set_workflow_state(before_schema["content"], state)
    mapped = set_block(mapped, "caseMeta", {"status": status})
    assert not validate_case_dict_data(parse_casedict(mapped)), (status, state)
print("7c. 全部 status 与 workflowState 映射 [OK]")

# 7d. errors.entries 生命周期、summary 一致性与结清证据
import copy
entry = {
    "title": "nonOrtho exceeds target", "stage": "mesh", "category": "meshQuality",
    "severity": "error", "status": "open", "blocking": "true",
    "message": "max nonOrtho 85", "relatedPaths": ["B-mesh/report.txt"],
    "fixOptions": {"reduceNonOrtho": {"type": "edit", "path": "system/blockMeshDict"}},
}
summary = {"total": 1, "open": 1, "blocking": 1, "resolved": 0, "lastErrorId": "error_0001"}
open_text = set_block(before_schema["content"], "errors", {"summary": summary, "entries": {"error_0001": entry}})
open_text = set_workflow_state(open_text, "C")
before_open = call("GET", f"/cases/{case_id}/caseDict", token=tok)
try:
    result = call("PUT", f"/cases/{case_id}/caseDict", {"content": open_text}, token=tok)
    raise AssertionError(f"开放阻断 entries 被接受: {result}")
except urllib.error.HTTPError as e:
    assert e.code == 409, e.code
    before_gate = call("GET", f"/cases/{case_id}/caseDict", token=tok)
    assert before_gate["content"] == before_open["content"]
    assert before_gate["dict_revision"] == before_open["dict_revision"]
    print("7d. 开放阻断 entries 409 且文件/revision 不变 [OK]")
finally:
    call("PUT", f"/cases/{case_id}/caseDict", {"content": before_open["content"]}, token=tok)

mismatch_summary = dict(summary, open=0, blocking=0)
mismatch = set_block(before_schema["content"], "errors", {"summary": mismatch_summary, "entries": {"error_0001": entry}})
reject_schema("entries 与 summary 不一致", mismatch)
fixed_without_resolution = copy.deepcopy(entry)
fixed_without_resolution["status"] = "fixed"
fixed_summary = dict(summary, open=0, blocking=0, resolved=1)
fixed_text = set_block(before_schema["content"], "errors", {"summary": fixed_summary, "entries": {"error_0001": fixed_without_resolution}})
reject_schema("fixed 缺 resolution", fixed_text)
verified_without_verification = copy.deepcopy(fixed_without_resolution)
verified_without_verification["status"] = "verified"
verified_without_verification["resolution"] = {"summary": "refined mesh"}
verified_text = set_block(before_schema["content"], "errors", {"summary": fixed_summary, "entries": {"error_0001": verified_without_verification}})
reject_schema("verified 缺 verification", verified_text)
invalid_severity = copy.deepcopy(entry)
invalid_severity["severity"] = "fatalistic"
invalid_severity_text = set_block(before_schema["content"], "errors", {"summary": summary, "entries": {"error_0001": invalid_severity}})
reject_schema("entry.severity 非法", invalid_severity_text)
unknown_id = set_block(before_schema["content"], "errors", {"summary": dict(summary, lastErrorId="error_9999"), "entries": {"error_0001": entry}})
reject_schema("lastErrorId 引用不存在", unknown_id)

fixed_entry = copy.deepcopy(entry)
fixed_entry["status"] = "fixed"
fixed_entry["resolution"] = {"summary": "refined mesh and reran checkMesh"}
fixed_valid_text = set_block(before_schema["content"], "errors", {"summary": fixed_summary, "entries": {"error_0001": fixed_entry}})
try:
    accepted = call("PUT", f"/cases/{case_id}/caseDict", {"content": fixed_valid_text}, token=tok)
    assert accepted["ok"]
    print("7d. fixed 有 resolution 可结清 [OK]")
finally:
    call("PUT", f"/cases/{case_id}/caseDict", {"content": before_schema["content"]}, token=tok)
verified_entry = copy.deepcopy(fixed_entry)
verified_entry["status"] = "verified"
verified_entry["verification"] = {"status": "passed", "evidence": "B-mesh/checkMesh.log"}
verified_valid_text = set_block(before_schema["content"], "errors", {"summary": fixed_summary, "entries": {"error_0001": verified_entry}})
try:
    accepted = call("PUT", f"/cases/{case_id}/caseDict", {"content": verified_valid_text}, token=tok)
    assert accepted["ok"] and accepted["workflow_state"] == "A"
    print("7d. verified 有 resolution+verification 可保存，阶段需独立证据 [OK]")
finally:
    call("PUT", f"/cases/{case_id}/caseDict", {"content": before_schema["content"]}, token=tok)

# 8. 运行时自检必须报告实际 v2406 与开发编译链
runtime = call("GET", "/runtime/capabilities", token=tok)
wsl = runtime["wsl"]
assert wsl["reachable"] and wsl["openfoam"] and wsl["wmake"], wsl
assert wsl.get("openfoam_version") == "v2406", wsl
assert "openfoam2406" in wsl.get("openfoam_path", ""), wsl
assert "openfoam2406" in wsl.get("wmake_path", ""), wsl
print("8. runtime capabilities 指向 v2406 且 wmake 可用 [OK]")

# 9. 未完整接入执行链的 S3 不得静默启动；fs 与显式受限旁路可用
from app.config import Settings, validate_storage_backend
validate_storage_backend(Settings(storage_backend="fs"))
try:
    validate_storage_backend(Settings(storage_backend="s3", allow_partial_s3=False))
    raise AssertionError("未受限声明的 S3 启动应被拒绝")
except RuntimeError as e:
    assert "S3" in str(e) and "ALLOW_PARTIAL_S3" in str(e)
validate_storage_backend(Settings(storage_backend="s3", allow_partial_s3=True))

# 真实启动也必须在 DB 初始化前拒绝，不触碰开发 PostgreSQL
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(prefix="vibeflow-storage-guard-") as tmp:
    db_path = Path(tmp) / "guard.db"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env["VIBEFLOW_STORAGE_BACKEND"] = "s3"
    env["VIBEFLOW_ALLOW_PARTIAL_S3"] = "false"
    env["VIBEFLOW_DATABASE_URL"] = "sqlite+aiosqlite:///" + db_path.as_posix()
    result = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=Path(__file__).resolve().parent, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30)
    output = result.stdout + result.stderr
    assert result.returncode != 0 and "VIBEFLOW_ALLOW_PARTIAL_S3" in output, output[-2000:]
    assert not db_path.exists(), "存储保护失败前不应初始化数据库"
print("9. 存储启动保护：fs 可用、默认 S3 拒绝、显式旁路可用 [OK]")

# 10. 新建 case 必须包含原始 16 块，且不得覆盖既有 caseDict
import time
before_existing = call("GET", f"/cases/{case_id}/caseDict", token=tok)
scaffold_name = f"scaffold-{time.time_ns()}"
created_scaffold = call("POST", f"/workspaces/{ws_id}/cases", {"name": scaffold_name}, token=tok)
scaffold_dict = call("GET", f"/cases/{created_scaffold['id']}/caseDict", token=tok)
expected_blocks = {
    "caseMeta", "intent", "physicalProblem", "geometry", "materials", "regions",
    "interfaces", "boundariesLoadsInitials", "mesh", "numericalMethod", "execution",
    "postProcessing", "report", "provenance", "errors", "extensions",
}
assert expected_blocks <= set(scaffold_dict["parsed"]), sorted(expected_blocks - set(scaffold_dict["parsed"]))
assert scaffold_dict["parsed"]["workflowState"] == "A"
after_existing = call("GET", f"/cases/{case_id}/caseDict", token=tok)
assert before_existing["content"] == after_existing["content"], "创建新 case 覆盖了既有 caseDict"
print("10. 新 case 含原始 16 块且既有 caseDict 不变 [OK]")

print("\n=== 全部通过 [OK] ===")
