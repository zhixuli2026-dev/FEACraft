"""可编辑 PPTX：模板、文本、形状、表格、图片、真实作业和 Open XML。"""
import base64
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

import httpx
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Inches

from app.services.ppt_builder import build_pptx

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


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
    for _ in range(240):
        state = call("GET", f"/jobs/{job_id}", token=token)
        if state.get("status") in ("succeeded", "failed", "cancelled", "interrupted"):
            log = call("GET", f"/jobs/{job_id}/log?offset=0", token=token)["chunk"]
            return state, log
        time.sleep(0.5)
    raise TimeoutError(job_id)


def template_bytes():
    stream = BytesIO()
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text_frame.text = "TemplateIntro"
    prs.core_properties.title = "TemplateTitle"
    prs.save(stream)
    return stream.getvalue()


TEMPLATE = template_bytes()


def document(output):
    return {
        "output": output,
        "title": "DeckTitle",
        "template": "template.pptx",
        "slides": [
            {"title": "EditableTitle", "bullets": ["Alpha", "Beta"], "shape": "roundedRectangle",
             "table": {"headers": ["Name", "Value"], "rows": [["A", 1], ["B", 2]]},
             "image": {"path": "pixel.png"}},
            {"title": "SecondSlide", "text": "Body text", "shape": "ellipse"},
        ],
    }


def assert_deck(data):
    prs = Presentation(BytesIO(data))
    assert len(prs.slides) == 3, len(prs.slides)
    assert prs.core_properties.title == "DeckTitle", prs.core_properties.title
    texts = [shape.text for slide in prs.slides for shape in slide.shapes if hasattr(shape, "text")]
    for expected in ("TemplateIntro", "EditableTitle", "Alpha", "SecondSlide", "Body text"):
        assert any(expected in text for text in texts), (expected, texts)
    assert any(shape.has_table for slide in prs.slides for shape in slide.shapes), "没有原生表格"
    assert any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for slide in prs.slides for shape in slide.shapes), "没有原生图片"
    with zipfile.ZipFile(BytesIO(data)) as archive:
        slide_xml = "".join(archive.read(name).decode("utf-8") for name in archive.namelist()
                            if name.startswith("ppt/slides/slide") and name.endswith(".xml"))
        assert "<a:tbl>" in slide_xml and "<p:pic>" in slide_xml
        assert 'prst="roundRect"' in slide_xml and 'prst="ellipse"' in slide_xml
        assert any(name.startswith("ppt/media/") for name in archive.namelist())


with tempfile.TemporaryDirectory(prefix="vibeflow-ppt-") as temporary:
    root = Path(temporary)
    (root / "template.pptx").write_bytes(TEMPLATE)
    (root / "pixel.png").write_bytes(PNG)
    original_hash = hashlib.sha256((root / "template.pptx").read_bytes()).hexdigest()
    result = build_pptx(root, document("out/deck.pptx"))
    assert result["slides"] == 3, result
    assert_deck((root / "out/deck.pptx").read_bytes())
    assert hashlib.sha256((root / "template.pptx").read_bytes()).hexdigest() == original_hash
    try:
        build_pptx(root, document("out/deck.pptx"))
        raise AssertionError("已有 PPTX 不应覆盖")
    except FileExistsError:
        pass
print("1. 纯函数模板保留、可编辑文本/形状/表格/图片和 Open XML [OK]")

token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"ppt-builder-{time.time_ns()}"}, token=token)
case_id = case["id"]
headers = {"Authorization": f"Bearer {token}"}
for path, data, mime in (("template.pptx", TEMPLATE, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
                         ("pixel.png", PNG, "image/png")):
    response = httpx.post(f"{B}/cases/{case_id}/upload?path={path}", headers=headers,
                          files={"file": (Path(path).name, data, mime)}, timeout=30, trust_env=False)
    assert response.status_code == 200, (response.status_code, response.text)
spec = document("E-report/build/demo.pptx")
job = call("POST", f"/cases/{case_id}/ppt:build", spec, token=token)
assert job.get("job_id"), job
state, log = wait_job(job["job_id"], token)
assert state["status"] == "succeeded", (state, log)
deck = call("GET", f"/cases/{case_id}/raw?path={spec['output']}", token=token, raw=True)
assert_deck(deck)
assert hashlib.sha256(call("GET", f"/cases/{case_id}/raw?path=template.pptx", token=token, raw=True)).hexdigest() == hashlib.sha256(TEMPLATE).hexdigest()
print(f"2. 真实 PPTX 作业 #{job['job_id']} 与源模板不变 [OK]")

for body, expected in [
    ({**spec, "output": spec["output"]}, 409),
    ({**spec, "output": "template.pptx"}, 400),
    ({**spec, "output": "../outside.pptx"}, 400),
    ({**spec, "output": "E-report/build/missing-image.pptx",
      "slides": [{**spec["slides"][0], "image": {"path": "missing.png"}}]}, 404),
]:
    rejected = call("POST", f"/cases/{case_id}/ppt:build", body, token=token)
    assert rejected.get("_err") == expected, (body["output"], rejected)
print("3. 重复输出、模板覆盖、越界和缺失图片均在作业前拒绝 [OK]")
print("\n=== 可编辑 PPTX 冒烟通过 ===")
