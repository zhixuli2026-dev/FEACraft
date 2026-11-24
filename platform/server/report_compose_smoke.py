"""报告自动章节：真实 caseDict/后处理证据 → compose → pandoc HTML/DOCX + 备份。"""
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")


def call(method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=60) as response:
            payload = response.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(180):
        time.sleep(1)
        state = call("GET", f"/jobs/{job_id}", token=token)
        if state["status"] in ("succeeded", "failed", "cancelled"):
            return state
    raise TimeoutError(job_id)


def main():
    token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    cases = call("GET", "/workspaces/1/cases", token=token)
    source = None
    for candidate in sorted(cases, key=lambda item: item["id"], reverse=True):
        pointer = call("GET", f"/cases/{candidate['id']}/file?path=D-post/data/latest-postprocess.json", token=token)
        if isinstance(pointer, dict) and "content" in pointer:
            source = candidate["id"], json.loads(pointer["content"])
            break
    assert source, "没有找到 postprocess 证据，请先运行 postprocess_smoke"
    source_id, pointer = source
    job_id = int(pointer["job_id"])
    manifest_path = f"D-post/data/postprocess-{job_id}/artifact-manifest.json"
    metrics_path = f"D-post/data/postprocess-{job_id}/metrics.json"
    manifest = json.loads(call("GET", f"/cases/{source_id}/file?path={manifest_path}", token=token)["content"])
    # Evidence belongs to the case that produced it; do not forge it by copying JSON.
    case_id = source_id

    composed = call("POST", f"/cases/{case_id}/report:compose", {"output": "E-report/report.md", "overwrite": True}, token=token)
    assert composed.get("path") == "E-report/report.md", composed
    assert composed.get("gate_reason") is None, composed
    assert composed["evidence"]["report_ready"] is True, composed
    assert len(composed["chapters"]) == 9, composed
    content = call("GET", f"/cases/{case_id}/file?path=E-report/report.md", token=token)["content"]
    for marker in ("volume_p.p@0.3", "volumeAt03", "verification", "validation", "../D-post/data/latest-postprocess.json", "../0-caseDict/caseDict"):
        assert marker in content, marker
    assert "can_claim_validation=`False`" in content
    print("1. caseDict+metrics+manifest 自动章节和证据链接 [OK]")

    build = call("POST", f"/cases/{case_id}/report:build", {}, token=token)
    assert build.get("job_id"), build
    assert wait_job(build["job_id"], token)["status"] == "succeeded", build
    html = call("GET", f"/cases/{case_id}/raw?path=E-report/build/report.html", token=token, raw=True)
    assert b"volume_p.p@0.3" in html and "证据索引".encode() in html
    docx = call("GET", f"/cases/{case_id}/raw?path=E-report/build/report.docx", token=token, raw=True)
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "volume_p.p@0.3" in xml and "volumeAt03" in xml
    print("2. pandoc HTML 内容、DOCX XML 与真实表格 [OK]")

    conflict = call("POST", f"/cases/{case_id}/report:compose", {"output": "E-report/report.md", "overwrite": False}, token=token)
    assert conflict.get("_err") == 409, conflict
    updated = call("POST", f"/cases/{case_id}/report:compose", {"output": "E-report/report.md", "overwrite": True}, token=token)
    assert updated.get("backup", "").startswith("E-report/compositions/"), updated
    backup = call("GET", f"/cases/{case_id}/file?path={updated['backup']}", token=token)
    assert "content" in backup and backup["content"].startswith("#"), updated
    print("3. 非空报告默认拒绝覆盖，显式覆盖先备份 [OK]")
    print("\n=== 报告自动章节与 DOCX 内容冒烟通过 ===")


main()
