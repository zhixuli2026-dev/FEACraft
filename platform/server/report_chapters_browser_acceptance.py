"""Real thermal engineering report chapters, with the public Edge workflow."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree
import zipfile


CASE = "E01 report chapter acceptance"
TITLE = "Engineering report chapter acceptance"
PROBLEM = "Compare the mean solid temperature with the analytic conduction solution."
ASSUMPTION = "Constant solid thermal properties and insulated sides."
LIMIT = "No experimental validation was performed."
MANUAL = "# User draft\n\nKeep this previous draft byte-for-byte.\n"
EDITOR_NOTE = "Reviewer observation: preserve the analytic reference separately."
UNCERTAINTY = "resolution_budget"
CHAPTER_IDS = ["problem", "assumptions", "geometry", "mesh", "methods", "results", "verification", "limitations", "evidence"]
DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def chapter_ids(markdown):
    rows = re.findall(r"^## ([0-9]+)\. [^\n]+ \{#([a-z]+)\}$", markdown, re.M)
    assert rows == [(str(index), key) for index, key in enumerate(CHAPTER_IDS, 1)], rows
    return [key for _, key in rows]


def verify_formats(markdown, html, docx, *, edited):
    chapter_ids(markdown)
    assert all(text in markdown for text in (PROBLEM, ASSUMPTION, LIMIT, UNCERTAINTY, "conductingSolid", "laplacianFoam"))
    assert (EDITOR_NOTE in markdown) is edited
    ids = re.findall(rb'<h2\s+id="([a-z]+)"[^>]*>', html)
    assert [name.decode() for name in ids] == CHAPTER_IDS, ids
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
    styles = []
    for paragraph in document.findall(".//w:p", DOCX_NS):
        style = paragraph.find("w:pPr/w:pStyle", DOCX_NS)
        if style is not None and style.get(f"{{{DOCX_NS['w']}}}val") == "Heading2":
            styles.append("".join(item.text or "" for item in paragraph.findall(".//w:t", DOCX_NS)))
    assert len(styles) == len(CHAPTER_IDS) and all(heading.startswith(str(index) + ". ")
                                                     for index, heading in enumerate(styles, 1)), styles
    word_text = " ".join(item.text or "" for item in document.findall(".//w:t", DOCX_NS))
    assert all(text in word_text for text in (PROBLEM, ASSUMPTION, LIMIT, UNCERTAINTY, "mean.T@1", "K"))
    assert (EDITOR_NOTE in word_text) is edited
    return {"chapter_ids": CHAPTER_IDS, "docx_heading2_count": len(styles),
            "html_sha256": sha(html), "docx_sha256": sha(docx)}


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings["root"])
    prefix = settings["prefix"]
    base = settings["base"]
    report = {}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings["channel"])
        context = edge.new_context(viewport={"width": 1500, "height": 940}, locale="zh-CN")
        context.add_init_script("if(location.origin===" + json.dumps(base) + ")localStorage.setItem('vf_token',"
                                + json.dumps(settings["token"]) + ");")
        page = context.new_page()
        page.set_default_timeout(30000)
        expect.set_options(timeout=30000)

        def call(path):
            response = context.request.get(base + "/api/v1" + path,
                                           headers={"Authorization": "Bearer " + settings["token"]})
            assert response.status == 200, (path, response.status)
            return response.json()

        def case_panel():
            page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^caseDict$")).click()

        def request_for(method, path):
            return lambda response: response.request.method == method and urlsplit(response.url).path == "/api/v1" + path

        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("[data-case-name=" + json.dumps(CASE) + "]").click()
            case_panel()
            compose = page.get_by_test_id("report-compose")
            page.once("dialog", lambda dialog: dialog.accept())
            matched = request_for("POST", prefix + "/report:compose")
            with page.expect_response(lambda res: matched(res) and not res.request.post_data_json["overwrite"]) as refused:
                with page.expect_response(lambda res: matched(res) and res.request.post_data_json["overwrite"], timeout=50000) as applied:
                    compose.click()
            assert refused.value.status == 409
            response = applied.value
            assert response.status == 200, (response.status, response.text()[:400])
            draft = response.json()
            assert draft["chapters"] == chapter_ids(draft["content"])
            assert draft["backup"] and draft["publication"]["applied"]
            expect(page.get_by_text("已生成 9 章，含证据链接；旧稿已备份", exact=True)).to_be_visible()
            report["draft_publication"] = draft["publication"]["id"]
            report["draft_backup"] = draft["backup"]

            folder = page.locator('[data-path="E-report"]')
            if folder.get_attribute("aria-expanded") == "false":
                folder.click()
            path = "E-report/report.md"
            page.locator('[data-path="E-report/report.md"]').click()
            tab = page.locator('[data-editor-path="E-report/report.md"]')
            expect(tab).to_have_attribute("data-active", "true")
            page.wait_for_function("window.monaco?.editor.getModels().length")
            assert page.evaluate("window.monaco.editor.getModels().at(-1).getValue()") == draft["content"]
            # Add a normal paragraph in limitations, preserving all chapter anchors.
            edited = draft["content"].replace("## 9. 证据索引与可复现性 {#evidence}", EDITOR_NOTE + "\n\n"
                                              + "## 9. 证据索引与可复现性 {#evidence}", 1)
            page.evaluate("(text) => window.monaco.editor.getModels().at(-1).setValue(text)", edited)
            with page.expect_response(request_for("PUT", prefix + "/file")) as saved:
                page.get_by_role("button", name="保存 (Ctrl+S)", exact=True).click()
            assert saved.value.status == 200 and saved.value.request.post_data_json["path"] == path
            assert saved.value.request.post_data_json["expected_sha256"] == sha(draft["content"].encode())
            expect(tab).to_have_attribute("data-dirty", "false")
            report["edited_draft_sha256"] = sha(edited.encode())

            case_panel()
            with page.expect_response(request_for("POST", prefix + "/report:build")) as built:
                page.get_by_test_id("report-build").click()
            assert built.value.status == 200, (built.value.status, built.value.text()[:300])
            report["build_job"] = built.value.json()["job_id"]
            expect(page.get_by_test_id("report-build-status")).to_contain_text("构建完成", timeout=135000)
            current = call(prefix + "/report/results")
            report["publication"] = current["publication"]["id"]
            controls = page.get_by_test_id("report-build-control")
            for ext in ("html", "docx"):
                case_panel()
                controls.get_by_role("button", name="report." + ext, exact=True).click()
                page.wait_for_function("""note => {
                    const doc = document.querySelector('[data-testid="document-shadow"]')?.shadowRoot;
                    return doc?.querySelectorAll('h2').length === 9 && doc.textContent.includes(note);
                }""", arg=EDITOR_NOTE, timeout=45000)
                if ext == "html":
                    ids = page.get_by_test_id("document-shadow").evaluate(
                        "node => [...node.shadowRoot.querySelectorAll('h2')].map(element => element.id)")
                    assert ids == CHAPTER_IDS
                    page.locator('[data-testid="document-heading"][data-target="limitations"]').click()
                    expect(page.get_by_test_id("document-notice")).to_contain_text("限制与未决项")
                    page.screenshot(path=str(root / "engineering-report-chapters.png"))
                with page.expect_download() as obtained:
                    page.get_by_test_id("document-download").click()
                downloaded = obtained.value
                data = Path(downloaded.path()).read_bytes()
                expected = current["manifest"]["artifacts"]["E-report/build/report." + ext]["sha256"]
                assert downloaded.suggested_filename == "report." + ext and sha(data) == expected
                report[ext + "_browser_download_sha256"] = sha(data)
            report["passed"] = True
        finally:
            (root / "e01-browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
            if not report.get("passed"):
                page.screenshot(path=str(root / "engineering-report-failure.png"))
            edge.close()


def verify(args):
    from app.services.casedict import parse_casedict, set_block
    from registered_cae_benchmarks import run_benchmark
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    account = {}

    class ReportServer(IsolatedServer):
        def __init__(self, _label):
            super().__init__("e01-engineering-report", app="report_chapter_fixture:app")
            (self.root / "report_chapter_fixture.py").write_text("from app.main import app\n"
                "from fastapi.staticfiles import StaticFiles\n"
                + "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n", encoding="utf8")
            account["root"] = self.root
            print("E01_EVIDENCE", self.root, flush=True)

        def call(self, method, path, body=None, **options):
            if method == "PUT" and hasattr(self, "prefix") and path == self.prefix + "/caseDict":
                content = body["content"]
                post = parse_casedict(content)["postProcessing"]
                derived = post["derivedQuantities"]
                derived["uncertainty"] = {UNCERTAINTY: {"metric": '"mean.T@1"', "standard": .2,
                    "coverage": 2, "source": "declared numerical resolution budget"}}
                content = set_block(content, "postProcessing", post)
                content = set_block(content, "intent", {"userIntent": PROBLEM, "assumptions": [ASSUMPTION],
                    "limitations": [LIMIT], "acceptanceCriteria": ["Match 350 K within the configured numerical tolerance."]})
                content = set_block(content, "materials", {"entries": {"conductingSolid": {"role": "solid", "conductivity": 1}}})
                content = set_block(content, "report", {"title": TITLE})
                body = {**body, "content": content}
            return super().call(method, path, body, **options)

        def create_case(self, _name):
            created = super().create_case(CASE)
            self.put("E-report/report.md", MANUAL)
            return created

    def measured(api, _flow, report):
        def get(path):
            return api.call("GET", api.prefix + path)

        def artifact(publication, path):
            query = urlencode({"publication_id": publication, "path": path})
            return api.call("GET", api.prefix + "/report/artifact?" + query, raw=True)

        def inspect(document, edited):
            source = next(path for path in document["manifest"]["artifacts"] if path.endswith(".md"))
            paths = [source, "E-report/build/report.html", "E-report/build/report.docx"]
            data = [artifact(document["publication"]["id"], path) for path in paths]
            assert all(sha(item) == document["manifest"]["artifacts"][path]["sha256"] for path, item in zip(paths, data))
            return verify_formats(data[0].decode(), data[1], data[2], edited=edited)

        initial = get("/report/results")
        assert initial["manifest"]["mode"] == "compose"
        report["original_structured_report"] = inspect(initial, False)
        assert api.call("GET", api.prefix + "/raw?path=E-report/report.md", raw=True) == MANUAL.encode()
        observations = get("/postprocess/results")["metrics"]
        mean = next(row for row in observations["metrics"] if row["name"] == "mean.T@1")
        assert math.isclose(mean["value"], 350, abs_tol=1e-6) and mean["unit"] == "K"
        assert observations["readiness"]["can_claim_verification"] and not observations["readiness"]["can_claim_validation"]
        uncertainty = next(row for row in observations["extractions"]["uncertainty"] if row["id"] == UNCERTAINTY)
        assert uncertainty["status"] == "pass" and uncertainty["unit"] == "K" and uncertainty["expanded"] == .4

        temporary = api.root / "browser-tmp"
        temporary.mkdir()
        subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
            input=json.dumps({"root": str(api.root), "base": api.base, "prefix": api.prefix, "token": api.token,
                              "channel": args.channel}), text=True, check=True, timeout=300,
            env=dict(os.environ, TMP=str(temporary), TEMP=str(temporary)))
        view = json.loads((api.root / "e01-browser.json").read_text("utf8"))
        current = get("/report/results")
        assert current["publication"]["id"] == view["publication"] and current["manifest"]["mode"] == "markdown"
        report["browser_report"] = inspect(current, True)
        assert current["manifest"]["inputs"]["E-report/report.md"]["sha256"] == view["edited_draft_sha256"]
        backup = api.call("GET", api.prefix + "/raw?" + urlencode({"path": view["draft_backup"]}), raw=True)
        assert backup == MANUAL.encode()
        historical = get("/report/results?" + urlencode({"publication_id": initial["publication"]["id"]}))
        assert inspect(historical, False) == report["original_structured_report"]
        mail = "e01-report-outsider@example.com"
        foreign = api.call("POST", "/auth/register", {"email": mail, "name": "Outsider", "password": "isolated-e01-123"})
        authorization = {"Authorization": "Bearer " + foreign["token"]}
        for path in ("/report/results", "/report/compose-result",
             "/report/artifact?" + urlencode({"publication_id": view["publication"], "path": "E-report/build/report.html"})):
            api.call("GET", api.prefix + path, expect=404, extra_headers=authorization)
        api.call("POST", api.prefix + "/report:compose", {"overwrite": True}, expect=404, extra_headers=authorization)
        report["e01"] = {"browser": view, "analytic_verification": True, "experimental_validation": False,
                         "expanded_uncertainty_K": uncertainty["expanded"], "acl_checked": True,
                         "old_draft_and_published_bytes_verified": True}

    report = run_benchmark("conduction", api_factory=ReportServer, measure_completed=measured)
    assert report["passed"] and report["e01"]["browser"]["passed"]
    print("REPORT_CHAPTERS_PASS", account["root"] / "benchmark.json", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dist", type=Path, required=True)
        parser.add_argument("--browser-python", type=Path, required=True)
        parser.add_argument("--channel", default="msedge")
        verify(parser.parse_args())
