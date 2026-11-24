"""PDF 页码：纯函数、真实作业、内容保留、独占输出和路径安全。"""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.request

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject

from app.services.pdf_pages import number_pdf

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
        with urllib.request.urlopen(req, data, timeout=30) as response:
            payload = response.read()
            return payload if raw else json.loads(payload)
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def wait_job(job_id, token):
    for _ in range(360):
        state = call("GET", f"/jobs/{job_id}", token=token)
        if state.get("status") in ("succeeded", "failed", "cancelled", "interrupted"):
            log = call("GET", f"/jobs/{job_id}/log?offset=0", token=token)["chunk"]
            return state, log
        time.sleep(0.5)
    raise TimeoutError(job_id)


def assert_numbered(pdf_bytes, expected_pages, original_texts, title):
    reader = PdfReader(BytesIO(pdf_bytes))
    assert len(reader.pages) == expected_pages
    assert reader.metadata.title == title, reader.metadata
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text()
        assert original_texts[index - 1] in text, (index, text)
        assert f"{index} / {expected_pages}" in text, (index, text)
        contents = page["/Contents"]
        last = contents[-1] if isinstance(contents, ArrayObject) else contents
        stream = last.get_object().get_data().decode("ascii")
        assert "/VFPageNumberFont 9 Tf" in stream and "0.35 g" in stream, stream
        assert " 22.00 Tm" in stream and f"({index} / {expected_pages}) Tj" in stream, stream


with tempfile.TemporaryDirectory(prefix="vibeflow-pdf-number-") as temporary:
    source = Path(temporary) / "source.pdf"
    output = Path(temporary) / "numbered.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_metadata({"/Title": "PURE_NUMBER"})
    with source.open("wb") as stream:
        writer.write(stream)
    assert number_pdf(source, output) == 1
    assert_numbered(output.read_bytes(), 1, [""], "PURE_NUMBER")
    try:
        number_pdf(source, output)
        raise AssertionError("已有输出不应被覆盖")
    except FileExistsError:
        pass
print("1. 纯函数页码、9pt/灰色/底部参数、元数据和独占输出 [OK]")

token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"pdf-number-{time.time_ns()}"}, token=token)
case_id = case["id"]
tex = ("\\documentclass{article}\n\\pdfinfo{/Title (NUMBER_SOURCE)}\n\\begin{document}\n"
       "Source page one.\\newpage\nSource page two.\n\\end{document}\n")
written = call("PUT", f"/cases/{case_id}/file", {"path": "E-report/latex/main.tex", "content": tex}, token=token)
assert written.get("ok"), written
build = call("POST", f"/cases/{case_id}/latex:build",
             {"main": "E-report/latex/main.tex", "recipe": "pdflatex"}, token=token)
state, log = wait_job(build["job_id"], token)
assert state["status"] == "succeeded", log[-2000:]
input_path = "E-report/latex/build/main.pdf"
source_bytes = call("GET", f"/cases/{case_id}/raw?path={input_path}", token=token, raw=True)
source_hash = hashlib.sha256(source_bytes).hexdigest()

job = call("POST", f"/cases/{case_id}/pdf:number", {"input": input_path}, token=token)
assert job.get("job_id"), job
assert job["output"] == "E-report/latex/build/main.numbered.pdf", job
state, log = wait_job(job["job_id"], token)
assert state["status"] == "succeeded", (state, log)
numbered = call("GET", f"/cases/{case_id}/raw?path={job['output']}", token=token, raw=True)
assert_numbered(numbered, 2, ["Source page one.", "Source page two."], "NUMBER_SOURCE")
after = call("GET", f"/cases/{case_id}/raw?path={input_path}", token=token, raw=True)
assert hashlib.sha256(after).hexdigest() == source_hash, "源 PDF 被修改"
print(f"2. 真实两页作业 #{job['job_id']}、原文字/页数/元数据保留、源哈希不变 [OK]")

for body, expected in [
    ({"input": input_path, "output": job["output"]}, 409),
    ({"input": input_path, "output": input_path}, 400),
    ({"input": "../outside.pdf"}, 400),
]:
    rejected = call("POST", f"/cases/{case_id}/pdf:number", body, token=token)
    assert rejected.get("_err") == expected, (body, rejected)
assert call("GET", f"/cases/{case_id}/raw?path={job['output']}", token=token, raw=True) == numbered
print("3. 重复输出 409、同源覆盖/越界 400，已有产物不变 [OK]")
print("\n=== PDF 页码冒烟通过 ===")
