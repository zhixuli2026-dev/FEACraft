"""caseDict 统一写入单元冒烟：Agent 提案也必须 parse → 写文件 → 更新镜像。"""
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

from app.services.casedict import (CASE_DICT_PATH, CASE_DICT_TEMPLATE, parse_casedict,
                                   set_block, set_workflow_state, update_case_dict)
from app.services.tools import ToolExecutor
from app.storage import _fs_path

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def api_call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data, timeout=15) as response:
        return json.loads(response.read())


def expect_400(action):
    try:
        action()
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
        return
    raise AssertionError("caseDict 变体路径应返回 400")


def block_fidelity_check():
    updated = set_block(CASE_DICT_TEMPLATE, "caseMeta",
                        {"discipline": "fluid", "analysisType": "transient"})
    assert "// caseName" in updated and "// discipline" in updated
    assert "// analysisType" in updated and "// executionProfile" in updated
    assert "workflowState A;" in updated
    assert "discipline fluid;" in updated and "analysisType transient;" in updated
    parse_casedict(updated)
    print("7. 新键追加时保留块内注释和 workflowState [OK]")

    updated2 = set_block(updated, "caseMeta", {"discipline": "thermal"})
    meta2 = parse_casedict(updated2)["caseMeta"]
    assert "analysisType" not in meta2
    assert meta2["discipline"] == "thermal"
    assert "// caseName" in updated2 and "// discipline" in updated2
    assert "workflowState A;" in updated2
    parse_casedict(updated2)
    print("8. 全量编辑只删目标键，不删注释 [OK]")

    updated3 = set_block(updated2, "caseMeta", {"discipline": "steady"}, replace_missing=False)
    meta3 = parse_casedict(updated3)["caseMeta"]
    assert meta3["discipline"] == "steady"
    assert "analysisType" not in meta3
    assert "// caseName" in updated3 and "// discipline" in updated3
    parse_casedict(updated3)
    print("9. 局部编辑不误删其他键/注释 [OK]")

    for block, entries in (("caseMeta", {"missing": 1}), ("missingBlock", {"x": 1})):
        try:
            set_block(updated3, block, entries, replace_missing=False)
            raise AssertionError("局部模式下缺失块/键应明确报错")
        except ValueError:
            pass
    print("10. 局部模式缺失块/键明确拒绝 [OK]")

    cleared = set_block(updated3, "caseMeta", {})
    assert parse_casedict(cleared)["caseMeta"] == {}
    assert "workflowState A;" in cleared
    print("11. 显式清空块仍保留 workflowState [OK]")


def gate_check():
    prefix = f"security-gate-{time.time_ns()}"
    case = SimpleNamespace(s3_prefix=prefix, workflow_state="A", dict_revision=0,
                           case_meta="{}", parse_status="ok")
    root = _fs_path(prefix, ".")
    try:
        executor = ToolExecutor(prefix, case_dict_writer=lambda content: update_case_dict(case, content))
        with_errors = set_block(CASE_DICT_TEMPLATE, "errors",
                                {"summary": {"open": 1, "blocking": 0}})
        with_errors = set_workflow_state(with_errors, "B")
        try:
            executor.apply_file_update(CASE_DICT_PATH, with_errors)
            raise AssertionError("open errors 不应允许推进 workflowState")
        except ValueError:
            pass
        assert case.workflow_state == "A" and case.dict_revision == 0
        assert not _fs_path(prefix, CASE_DICT_PATH).exists()
        print("12. open errors 阻止推进且文件/revision 不变 [OK]")

        cleared = set_block(with_errors, "errors", {"summary": {"open": 0, "blocking": 0}})
        try:
            executor.apply_file_update(CASE_DICT_PATH, cleared)
            raise AssertionError("结清错误也不能绕过显式阶段确认")
        except ValueError:
            pass
        cleared = set_workflow_state(cleared, "A")
        executor.apply_file_update(CASE_DICT_PATH, cleared)
        assert case.workflow_state == "A" and case.dict_revision == 1
        blocking = set_block(cleared, "errors", {"summary": {"open": 0, "blocking": 1}})
        blocking = set_workflow_state(blocking, "C")
        try:
            executor.apply_file_update(CASE_DICT_PATH, blocking)
            raise AssertionError("blocking errors 不应允许推进 workflowState")
        except ValueError:
            pass
        assert case.workflow_state == "A" and case.dict_revision == 1
        print("13. 结清错误仍需阶段确认，普通参数保存成功 [OK]")
    finally:
        if root.exists():
            shutil.rmtree(root)


def api_mirror_check():
    token = api_call("POST", "/auth/login",
                     {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    workspace = api_call("GET", "/workspaces", token=token)[0]
    case_id = api_call("POST", f"/workspaces/{workspace['id']}/cases",
                       {"name": f"casedict-sync-{time.time_ns()}"}, token=token)["id"]
    before = api_call("GET", f"/cases/{case_id}/caseDict", token=token)
    gated = set_block(before["content"], "errors", {"summary": {"open": 1, "blocking": 0}})
    try:
        api_call("PUT", f"/cases/{case_id}/caseDict",
                 {"content": gated, "workflow_state": "B"}, token=token)
        raise AssertionError("REST open errors 应返回 409")
    except urllib.error.HTTPError as e:
        assert e.code == 409, e.code
    mirror = api_call("GET", f"/cases/{case_id}", token=token)
    assert mirror["workflow_state"] == before["workflow_state"]
    assert mirror["dict_revision"] == before["dict_revision"]
    cleared = set_block(gated, "errors", {"summary": {"open": 0, "blocking": 0}})
    allowed = api_call("PUT", f"/cases/{case_id}/caseDict",
                       {"content": cleared, "workflow_state": "A"}, token=token)
    assert allowed["workflow_state"] == "A"
    print("14. REST 阶段旁路 409，同阶段保存成功 [OK]")
    try:
        result = api_call("PUT", f"/cases/{case_id}/caseDict/block", {
            "block": "caseMeta",
            "entries": {"discipline": "fluid", "analysisType": "transient"}}, token=token)
        assert result["dict_revision"] == allowed["dict_revision"] + 1, result
        mirror = api_call("GET", f"/cases/{case_id}", token=token)
        meta = json.loads(mirror["case_meta"])
        assert meta.get("discipline") == "fluid", mirror
        assert meta.get("analysisType") == "transient", mirror
        assert mirror["workflow_state"] == result["workflow_state"]
        assert mirror["dict_revision"] == result["dict_revision"]

        for variant in ("./0-caseDict/caseDict", "0-caseDict//caseDict"):
            expect_400(lambda variant=variant: api_call(
                "PUT", f"/cases/{case_id}/file",
                {"path": variant, "content": "broken { {"}, token=token))
        expect_400(lambda: api_call(
            "DELETE", f"/cases/{case_id}/file?path={urllib.parse.quote('./0-caseDict/caseDict', safe='')}",
            token=token))

        boundary = "----vibeflow-casedict-smoke"
        payload = (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"caseDict\"\r\n"
            "Content-Type: text/plain\r\n\r\n"
            "broken { {\r\n"
            f"--{boundary}--\r\n").encode()
        upload_path = urllib.parse.quote("./0-caseDict/caseDict", safe="")
        req = urllib.request.Request(
            f"{B}/cases/{case_id}/upload?path={upload_path}",
            data=payload, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Authorization": f"Bearer {token}"})
        expect_400(lambda: urllib.request.urlopen(req, timeout=15))
    finally:
        api_call("PUT", f"/cases/{case_id}/caseDict",
                 {"content": before["content"]}, token=token)
    print("5. REST 块写与 DB 镜像一致、原文恢复 [OK]")
    print("6. 普通写入/上传/删除的 caseDict 变体旁路均拒绝 [OK]")


def main():
    prefix = f"security-casedict-{time.time_ns()}"
    case = SimpleNamespace(s3_prefix=prefix, workflow_state="A", dict_revision=0,
                           case_meta="{}", parse_status="ok")
    root = _fs_path(prefix, ".")
    try:
        executor = ToolExecutor(
            prefix, case_dict_writer=lambda content: update_case_dict(case, content))
        executor.apply_file_update(CASE_DICT_PATH, CASE_DICT_TEMPLATE)
        path = _fs_path(prefix, CASE_DICT_PATH)
        assert path.is_file() and case.dict_revision == 1
        assert case.workflow_state == "A" and json.loads(case.case_meta) == {}
        print("1. Agent 整文件提案走统一写入 [OK]")

        changed = set_block(CASE_DICT_TEMPLATE, "caseMeta",
                            {"discipline": "fluid", "analysisType": "transient"})
        executor.apply_file_update(CASE_DICT_PATH, changed)
        assert case.workflow_state == "A" and case.dict_revision == 2
        assert json.loads(case.case_meta)["discipline"] == "fluid"
        executor.apply_text_replacement(CASE_DICT_PATH, "discipline fluid;", "discipline thermal;")
        assert case.workflow_state == "A" and case.dict_revision == 3
        assert json.loads(case.case_meta)["discipline"] == "thermal"
        assert "workflowState A;" in path.read_text(encoding="utf-8")
        print("2. Agent 精确替换同步 state/meta/revision [OK]")

        before = path.read_bytes()
        try:
            executor.apply_file_update(CASE_DICT_PATH, "broken { {")
            raise AssertionError("非法 caseDict 不应写入")
        except ValueError:
            pass
        assert path.read_bytes() == before and case.dict_revision == 3
        assert case.workflow_state == "A"
        try:
            ToolExecutor(prefix).apply_file_update(CASE_DICT_PATH, CASE_DICT_TEMPLATE)
            raise AssertionError("无同步回调的 caseDict 写入应被拒绝")
        except ValueError:
            pass
        assert path.read_bytes() == before and case.dict_revision == 3
        print("3. 非法写入与无镜像旁路均拒绝 [OK]")

        executor.apply_file_update("D-post/notes.md", "ordinary file")
        assert "ordinary file" in _fs_path(prefix, "D-post/notes.md").read_text(encoding="utf-8")
        print("4. 普通文件提案不受影响 [OK]")
    finally:
        if root.exists():
            shutil.rmtree(root)

    block_fidelity_check()
    gate_check()
    api_mirror_check()
    print("\n=== caseDict 统一写入冒烟通过 ===")


if __name__ == "__main__":
    main()
