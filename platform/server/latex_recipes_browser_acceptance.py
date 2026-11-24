"""Three real LaTeX recipes, biber, browser recovery and immutable PDF history."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
import uuid


CASE = "E03 LaTeX recipe acceptance"
MAIN = "E-report/latex/main.tex"
BIB = "E-report/latex/refs.bib"
PDF = "E-report/latex/build/main.pdf"
BBL = "E-report/latex/build/main.bbl"
TRANSCRIPT = "E-report/latex/build/main.latex.log"
XETEX = """\\documentclass[UTF8,fontset=fandol]{ctexart}
\\usepackage[backend=biber,style=authoryear]{biblatex}
\\addbibresource{refs.bib}
\\begin{document}
真实编译验收. XeRecipeFixture \\cite{e03calibrated}.
\\printbibliography
\\end{document}
"""
BIB_TEXT = """@article{e03calibrated,
  author={Example, Ada}, title={Calibrated Evidence}, journal={Validation Letters},
  year={2026}, volume={1}, pages={1--3},
}
"""
LUA = "\\documentclass{article}\n\\begin{document}\nLuaRecipeFixture.\n\\end{document}\n"
BROKEN = "\\documentclass{article}\n\\begin{document}\nExpected failure\n\\EthreeUndefinedControl\n\\end{document}\n"
FIXED = BROKEN.replace("\\EthreeUndefinedControl", "PdfRecipeFixture recovered")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def copy_previous_run(previous, target):
    source = previous.resolve(strict=True)
    assert source.parent == target.parent and source.name.startswith("e03-real-latex-")
    prior = json.loads((source / "latex-browser.json").read_text("utf8"))
    assert json.loads((source / "cleanup-independent.json").read_text("utf8"))["passed"]
    assert prior["failed_build_preserved_previous_publication"]
    first, failed = prior["jobs"]
    assert [(row["recipe"], row["status"]) for row in (first, failed)] == [
        ("lualatex", "succeeded"), ("pdflatex", "failed")]
    artifact = json.loads((source / "lua-publication-independent.json").read_text("utf8"))
    assert artifact["publication"] == first["publication"] and artifact["path"] == PDF
    files = source / "files"
    assert not any(path.is_symlink() for path in files.rglob("*"))
    shutil.copytree(files, target / "files")
    preserved = target / artifact["physical_file"]
    assert preserved.is_relative_to(target / "files") and sha(preserved.read_bytes()) == artifact["sha256"]
    database = (source / "test.sqlite").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database, uri=True)) as existing:
        assert existing.execute("select id,status from jobs order by id").fetchall() == [
            (first["job"], "succeeded"), (failed["job"], "failed")]
        case, account, email, key = existing.execute(
            "select request.case_id,users.id,users.email,request.idempotency_key from job_submission_requests request "
            "join users on users.id=request.created_by where request.job_id=?", (failed["job"],)).fetchone()
        assert re.fullmatch(r"[0-9a-f]{32}@example\.com", email) and str(uuid.UUID(key)) == key
        with closing(sqlite3.connect(target / "test.sqlite")) as copied:
            existing.backup(copied)
    return {"prior": source.name, "jobs": prior["jobs"], "old_pdf_sha256": artifact["sha256"],
            "account": account, "email": email, "case": case,
            "submission": {"key": key, "main": MAIN, "recipe": "pdflatex", "jobId": failed["job"]}}


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings["root"])
    base = settings["base"]
    prefix = settings["prefix"]
    endpoint = prefix + "/latex:build"
    previous = settings.get("previous")
    result = {"jobs": [], "admission_retries": 0}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings["channel"])
        context = edge.new_context(viewport={"width": 1530, "height": 970}, locale="zh-CN")
        context.add_init_script("if(location.origin===" + json.dumps(base) + ")localStorage.setItem('vf_token',"
                                + json.dumps(settings["token"]) + ");")
        if previous:
            hint = f"vf-latex-build-v1:{previous['account']}:{previous['case']}"
            context.add_init_script("if(location.origin===" + json.dumps(base) + ")sessionStorage.setItem("
                + json.dumps(hint) + "," + json.dumps(json.dumps(previous["submission"])) + ");")
        page = context.new_page()
        page.set_default_timeout(45000)
        expect.set_options(timeout=45000)
        build = page.get_by_test_id("latex-build")
        status = page.get_by_test_id("latex-status")
        recipe = page.get_by_test_id("latex-recipe")
        submissions = []
        runtime = {"started": 0, "finished": 0, "responses": []}
        capability_path = "/runtime/capabilities"

        def api(method, path, *, data=None, expect_code=200):
            result = context.request.fetch(base + "/api/v1" + path, method=method, data=data,
                headers={"Authorization": "Bearer " + settings["token"]}, timeout=50000)
            assert result.status == expect_code, (method, path, result.status, result.text()[:240])
            return result.json()

        def matches(request, method, path):
            return request.method == method and urlsplit(request.url).path == "/api/v1" + path

        def track(request):
            if matches(request, "POST", endpoint):
                submissions.append(request)
            if matches(request, "GET", capability_path):
                runtime["started"] += 1

        def finish_runtime(request):
            if matches(request, "GET", capability_path):
                runtime["finished"] += 1

        def capture_runtime_response(response):
            if matches(response.request, "GET", capability_path):
                runtime["responses"].append(response.status)

        def ready_for_compile():
            deadline = time.monotonic() + 140
            while not runtime["started"] or runtime["finished"] < runtime["started"]:
                assert time.monotonic() < deadline, runtime
                page.wait_for_timeout(200)
            assert runtime["responses"], runtime
            result["runtime_checks_before_compile"] = dict(runtime)

        def select_case():
            page.locator("[data-case-name=" + json.dumps(CASE) + "]").click()
            show_case_panel()

        def show_case_panel():
            page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^caseDict$")).click()
            expect(build).to_be_visible()

        def is_busy(response):
            detail = response.json().get("detail") if response.status == 409 else None
            return isinstance(detail, dict) and detail.get("code") == "storage_quota_busy"

        def click_submit():
            deadline = time.monotonic() + 30
            while True:
                with page.expect_response(lambda response: matches(response.request, "POST", endpoint), timeout=145000) as sent:
                    build.click()
                response = sent.value
                if not is_busy(response):
                    break
                assert time.monotonic() < deadline
                result["admission_retries"] += 1
                page.wait_for_timeout(300)
            assert response.status == 200, (response.status, response.text()[:300])
            return response

        def launch(engine):
            show_case_panel()
            recipe.select_option(engine)
            response = click_submit()
            assert response.request.post_data_json == {"main": MAIN, "recipe": engine}
            return response.json()

        def wait(job, phase, label, engine):
            # The registered job can use its full 300-second budget before publication settles.
            expect(status).to_contain_text(re.compile(r"编译(?:完成|失败|已取消|已中断)"), timeout=370000)
            expect(status).to_contain_text(label, timeout=2500)
            expect(build).to_be_enabled(timeout=35000)
            current = api("GET", "/jobs/" + str(job["job_id"]))
            assert current["status"] == phase and not current["completion_pending"]
            stop = current["attempt"]["process_stop"]
            assert stop["verified"] and stop["scope_empty"]
            children = current["attempt"]["child_processes"]["children"]
            assert children and all(child["stop"]["verified"] and child["stop"]["scope_empty"] for child in children.values())
            result["jobs"].append({"recipe": engine, "job": job["job_id"], "publication": job["publication_id"],
                "status": phase, "children": len(children)})
            print(f"E03_LATEX_JOB {job['job_id']} {engine} {phase} children={len(children)}", flush=True)

        def set_source(content):
            api("PUT", prefix + "/file", data={"path": MAIN, "content": content})

        def xelatex_recovery():
            recipe.select_option("xelatex")
            original = {}

            def lose_reply(route):
                deadline = time.monotonic() + 25
                while True:
                    real = route.fetch(timeout=65000)
                    if real.status == 200:
                        original.update(job=real.json(), key=route.request.headers["idempotency-key"])
                        route.abort("connectionfailed")
                        return
                    if not is_busy(real) or time.monotonic() >= deadline:
                        route.fulfill(response=real)
                        return
                    result["admission_retries"] += 1
                    time.sleep(.25)

            page.route("**/latex:build", lose_reply, times=1)
            build.click()
            expect(status).to_contain_text("提交结果待确认", timeout=75000)
            assert original and original["key"]
            expect(build).to_have_text("确认原编译提交")
            confirmed = click_submit()
            first = confirmed.json()
            assert first["job_id"] == original["job"]["job_id"]
            assert confirmed.request.headers["idempotency-key"] == original["key"]
            assert api("GET", "/jobs/" + str(first["job_id"]))["status"] in {"queued", "running"}
            posts_before_reload = len(submissions)
            page.reload(wait_until="domcontentloaded")
            select_case()
            expect(recipe).to_have_value("xelatex")
            wait(first, "succeeded", "编译完成", "xelatex")
            assert len(submissions) == posts_before_reload
            assert len(api("GET", prefix + "/jobs")) == 1
            result["same_request_recovery_without_resubmit_on_reload"] = True

        try:
            page.on("request", track)
            page.on("requestfinished", finish_runtime)
            page.on("requestfailed", finish_runtime)
            page.on("response", capture_runtime_response)
            page.goto(base, wait_until="domcontentloaded")
            select_case()
            if previous:
                first, failed = previous["jobs"]
                result["jobs"].append(first)
                wait({"job_id": failed["job"], "publication_id": failed["publication"]}, "failed", "编译失败", "pdflatex")
                assert not submissions
                preceding = first["publication"]
                result["resumed_failed_job_for_diagnostic"] = failed["job"]
            else:
                ready_for_compile()
                page.get_by_test_id("latex-main").select_option(MAIN)
                if settings["phase"] == "all":
                    xelatex_recovery()
                set_source(LUA)
                second = launch("lualatex")
                wait(second, "succeeded", "编译完成", "lualatex")
                set_source(BROKEN)
                failed = launch("pdflatex")
                wait(failed, "failed", "编译失败", "pdflatex")
                preceding = second["publication_id"]
            assert api("GET", prefix + "/latex/results")["publication"]["id"] == preceding
            result["failed_build_preserved_previous_publication"] = True
            diagnostic = page.locator('[data-testid="latex-diagnostic"][data-severity="error"][data-line="4"]'
                                      '[data-file="E-report/latex/main.tex"]').first
            expect(diagnostic).to_contain_text("Undefined control sequence")
            diagnostic.click()
            tab = page.locator('[data-editor-path="E-report/latex/main.tex"]')
            expect(tab).to_have_attribute("data-active", "true")
            expect(page.get_by_test_id("editor-cursor-line")).to_have_text("L4")
            page.screenshot(path=str(root / "latex-diagnostic-open.png"))
            page.evaluate("(text) => window.monaco.editor.getModels().at(-1).setValue(text)", FIXED)
            with page.expect_response(lambda response: matches(response.request, "PUT", prefix + "/file")) as saved:
                page.get_by_role("button", name="保存 (Ctrl+S)", exact=True).click()
            assert saved.value.status == 200 and saved.value.request.post_data_json["expected_sha256"] == sha(BROKEN.encode())
            expect(tab).to_have_attribute("data-dirty", "false")
            result["real_error_line_and_corrected_source"] = {"line": 4, "source_sha256": sha(FIXED.encode())}
            ready_for_compile()
            recovered = launch("pdflatex")
            wait(recovered, "succeeded", "编译完成", "pdflatex")

            for directory in ("E-report", "E-report/latex", "E-report/latex/build"):
                entry = page.locator("[data-path=" + json.dumps(directory) + "]")
                if entry.get_attribute("aria-expanded") == "false":
                    entry.click()
            page.locator("[data-path=" + json.dumps(PDF) + "]").click()
            expect(page.get_by_test_id("pdf-viewer")).to_be_visible()
            expect(page.locator(".textLayer")).to_contain_text("PdfRecipeFixture recovered", timeout=45000)
            with page.expect_download() as file:
                page.get_by_test_id("pdf-download").click()
            download = file.value
            assert download.suggested_filename == "main.pdf"
            digest = sha(Path(download.path()).read_bytes())
            latest = api("GET", prefix + "/latex/results")
            assert latest["publication"]["id"] == recovered["publication_id"]
            assert latest["manifest"]["artifacts"][PDF]["sha256"] == digest
            result["browser_pdf_sha256"] = digest
            page.screenshot(path=str(root / "latex-pdf-editor.png"))
            result["passed"] = True
        finally:
            (root / "latex-browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            if not result.get("passed"):
                page.screenshot(path=str(root / "latex-browser-failure.png"))
            edge.close()


def verify(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("e03-real-latex", app="latex_recipes_fixture:app")
    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    previous = copy_previous_run(args.resume_pdf_from, api.root) if args.resume_pdf_from else None
    stable_key = uuid.uuid4().hex
    (api.root / "latex_recipes_fixture.py").write_text("import os\n"
        + "os.environ['VIBEFLOW_JWT_SECRET'] = " + repr(stable_key) + "\n"
        + "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n", encoding="utf8")
    temporary = api.root / "browser-tmp"
    temporary.mkdir()
    all_engines = args.phase == "all"
    early_engine, early_source = ("xelatex", XETEX) if all_engines else ("lualatex", LUA)
    report = {"passed": False, "phase": args.phase, "model_calls": 0, "mock_llm_calls": 0,
              "storage": "fs", "database": "sqlite"}
    if previous:
        report["previous_run"] = previous["prior"]
    print("E03_LATEX_EVIDENCE", api.root, flush=True)

    def artifact(published, path):
        query = urlencode({"publication_id": published, "path": path})
        return api.call("GET", api.prefix + "/latex/artifact?" + query, raw=True)

    def outcome(published):
        return api.call("GET", api.prefix + "/latex/results?" + urlencode({"publication_id": published}))

    def document(row, marker):
        info = outcome(row["publication"])
        manifest = info["manifest"]
        assert manifest["recipe"] == row["recipe"] and manifest["job_id"] == row["job"]
        runtime = info["toolchain"]["native"]["runtime"]
        binaries = {"xelatex": ("xetex",), "pdflatex": ("pdftex",), "lualatex": ("luatex", "luahbtex")}
        engine = runtime[row["recipe"]]["version"].splitlines()[0]
        assert any(name in engine.lower() for name in binaries[row["recipe"]]), engine
        data = artifact(row["publication"], PDF)
        assert data.startswith(b"%PDF") and sha(data) == manifest["artifacts"][PDF]["sha256"]
        local = api.root / (row["recipe"] + ".pdf")
        local.write_bytes(data)
        assert local.drive.upper() == "D:"
        linux = "/mnt/d" + local.as_posix()[2:]
        result = subprocess.run(["wsl.exe", "-d", "vibeflow-linux", "--user", "nobody", "--exec",
            "pdftotext", linux, "-"], capture_output=True, check=True, timeout=30)
        text = result.stdout.decode("utf8")
        assert marker in text
        if row["recipe"] == "xelatex":
            assert "真实编译验收" in re.sub(r"\s+", "", text) and "Calibrated Evidence" in text
            bibliography = artifact(row["publication"], BBL)
            log = artifact(row["publication"], TRANSCRIPT)
            assert b"e03calibrated" in bibliography and b"biber" in log.lower()
            assert sha(bibliography) == manifest["artifacts"][BBL]["sha256"]
        else:
            assert BBL not in manifest["artifacts"]
        return {"publication": row["publication"], "sha256": sha(data), "pages": manifest["pages"],
                "engine": engine, "original_pdf_marker": marker}

    try:
        with api:
            if previous:
                api.token = api.call("POST", "/auth/login", {"email": previous["email"],
                    "password": "isolated-test-123"})["token"]
                api.case_id = previous["case"]
                api.prefix = f"/cases/{api.case_id}"
                assert sha(artifact(previous["jobs"][0]["publication"], PDF)) == previous["old_pdf_sha256"]
            else:
                api.create_case(CASE)
                api.put(MAIN, early_source)
                if all_engines:
                    api.put(BIB, BIB_TEXT)
                api.call("POST", api.prefix + "/latex:build", {"main": MAIN, "recipe": "unsupported"}, expect=400)
            case = api.call("GET", api.prefix)
            try:
                subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                    input=json.dumps({"root": str(api.root), "base": api.base, "prefix": api.prefix,
                                      "token": api.token, "channel": args.channel, "phase": args.phase,
                                      "previous": previous}), text=True, check=True, timeout=1150 if all_engines else 850,
                    env=dict(os.environ, TEMP=str(temporary), TMP=str(temporary)))
            finally:
                for row in api.call("GET", api.prefix + "/jobs"):
                    if row["status"] in {"queued", "running"}:
                        api.call("POST", f"/jobs/{row['id']}:cancel", {}, expect=(200, 409))
                        deadline = time.monotonic() + 80
                        while time.monotonic() < deadline:
                            current = api.call("GET", f"/jobs/{row['id']}")
                            if current["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                                break
                            time.sleep(.25)
            view = json.loads((api.root / "latex-browser.json").read_text("utf8"))
            assert view["passed"]
            assert len(api.call("GET", api.prefix + "/jobs")) == (4 if all_engines else 3)
            successes = {row["recipe"]: row for row in view["jobs"] if row["status"] == "succeeded"}
            report["browser"] = view
            markers = {"xelatex": "XeRecipeFixture", "lualatex": "LuaRecipeFixture", "pdflatex": "PdfRecipeFixture recovered"}
            report["documents"] = {engine: document(row, markers[engine]) for engine, row in successes.items()}
            persisted_token = api.token
            guest_email = "latex-reader@example.com"
            account = api.call("POST", "/auth/register", {"email": guest_email, "name": "LaTex reader",
                               "password": "isolated-latex-e03-123"})
            guest = {"Authorization": "Bearer " + account["token"]}
            early = successes[early_engine]["publication"]
            read = "/latex/artifact?" + urlencode({"publication_id": early, "path": PDF})
            api.call("GET", api.prefix + read, expect=404, extra_headers=guest)
            api.call("POST", api.prefix + "/latex:build", {"main": MAIN, "recipe": "xelatex"}, expect=404, extra_headers=guest)
            org = api.call("GET", "/org/mine")["org"]["id"]
            api.call("POST", f"/org/{org}/members", {"email": guest_email, "role": "viewer"}, expect=201)
            api.call("POST", f"/org/{org}/invitation:accept", {}, extra_headers=guest)
            api.call("PUT", f"/workspaces/{case['workspace_id']}/sharing", {"org_id": org, "role": "viewer"})
            assert sha(api.call("GET", api.prefix + read, raw=True, extra_headers=guest)) == report["documents"][early_engine]["sha256"]
            api.call("POST", api.prefix + "/latex:build", {"main": MAIN, "recipe": "xelatex"}, expect=404, extra_headers=guest)
            report["acl"] = True
        first_exit = api.proc.poll() is not None
        with api:
            assert api.token == persisted_token
            for engine, item in report["documents"].items():
                assert sha(artifact(item["publication"], PDF)) == item["sha256"]
            assert outcome(successes["pdflatex"]["publication"])["publication"]["current"]
            if all_engines:
                assert b"e03calibrated" in artifact(early, BBL)
            source = "/latex/source?" + urlencode({"publication_id": early, "path": MAIN})
            assert api.call("GET", api.prefix + source, raw=True) == early_source.encode()
            report["cold_restart"] = {"pdfs": sorted(report["documents"]), "historical_source": early_engine,
                                      "historical_bbl": all_engines}
        report["passed"] = first_exit and api.proc.poll() is not None
    finally:
        report["api_exited"] = api.proc is not None and api.proc.poll() is not None
        (api.root / "latex-recipes-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    assert report["passed"]
    print("E03_LATEX_PASS", api.root / "latex-recipes-result.json", flush=True)


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
        parser.add_argument("--phase", choices=("all", "lua-pdf"), default="all")
        parser.add_argument("--resume-pdf-from", type=Path, help="Copy the completed real Lua and failed PDF receipt before the browser recovery")
        args = parser.parse_args()
        if args.resume_pdf_from and args.phase != "lua-pdf":
            parser.error("--resume-pdf-from requires --phase lua-pdf")
        verify(args)
