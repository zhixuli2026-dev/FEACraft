"""Real Edge, KaTeX and Monaco formula palette acceptance on an isolated API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit


CASE = "E05 Formula Palette acceptance"
TEX = "E-report/latex/formulas.tex"
NOTES = "E-report/notes.md"
BASELINE = "radian = INLINE_SLOT\nDISPLAY_SLOT\nLABELED_SLOT\nEQUATION_SLOT\n"
NOTE_TEXT = "# Formula notes\n\nMarkdown preview keeps the file unchanged.\n"
SYMBOLS = (
    ("希腊小写", "α", r"\alpha"),
    ("希腊大写", "Γ", r"\Gamma"),
    ("运算/关系", "≤", r"\leq"),
    ("结构", "a/b", r"\frac{a}{b}"),
    ("矩阵/括号", "bmatrix", r"\begin{bmatrix} a & b \\ c & d \end{bmatrix}"),
    ("CFD 常用", "∇²p", r"\nabla^{2} p"),
)
INLINE = r"\alpha+\Gamma\leq x"
DISPLAY = r"\frac{1}{2}\rho\lVert U\rVert^{2}"
LABELED = r"\nabla^{2} p = \rho"
EQUATION = r"\mathbf{u}\cdot\mathbf{n}=0"
EXPECTED = (BASELINE.replace("INLINE_SLOT", f"${INLINE}$")
            .replace("DISPLAY_SLOT", f"$$\n{DISPLAY}\n$$")
            .replace("LABELED_SLOT", f"\\begin{{equation}}\n{LABELED}\n\\label{{eq:pressure_1}}\n\\end{{equation}}")
            .replace("EQUATION_SLOT", f"\\begin{{equation}}\n{EQUATION}\n\\end{{equation}}"))


def sha(value):
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings["root"])
    base = settings["base"]
    file_route = "/api/v1" + settings["prefix"] + "/file"
    result = {"passed": False, "categories": [], "unexpected_model_requests": 0, "quota_retries": 0}

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings["channel"])

        def session(token):
            context = edge.new_context(viewport={"width": 1680, "height": 970}, locale="zh-CN")
            context.add_init_script("if(location.origin===" + json.dumps(base) + ")localStorage.setItem('vf_token',"
                                    + json.dumps(token) + ");")
            page = context.new_page()
            page.set_default_timeout(25000)
            page.on("request", add_model_attempt)
            return context, page

        def add_model_attempt(request):
            path = urlsplit(request.url).path
            if request.method == "POST" and re.match(r"/api/v1/(?:llm(?:/|:)|chat(?:/|:)|agents?(?:/|:))", path):
                result["unexpected_model_requests"] += 1

        def choose_case(page):
            workspace = page.get_by_role("combobox", name="当前工作区")
            workspace.select_option(str(settings["workspace"]))
            case = page.locator("[data-case-name=" + json.dumps(CASE) + "]")
            case.click()
            expect(case).to_have_attribute("aria-pressed", "true")

        def formula(page):
            page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^公式调色板$")).click()
            draft = page.get_by_role("textbox", name="LaTeX 公式源码")
            expect(draft).to_be_visible()
            return draft

        def open_file(page, file):
            path = Path(file)
            for parent in list(path.parents)[-2::-1]:
                folder = page.locator("[data-path=" + json.dumps(parent.as_posix()) + "]")
                if folder.get_attribute("aria-expanded") == "false":
                    folder.click()
            page.locator("[data-path=" + json.dumps(file) + "]").click()
            tab = page.locator("[data-editor-path=" + json.dumps(file) + "]")
            expect(tab).to_have_attribute("data-active", "true")
            return tab

        def model_value(page):
            return page.evaluate("window.monaco.editor.getEditors().at(-1)?.getModel()?.getValue()")

        def select_token(page, token):
            page.evaluate("""(token) => {
                const editor = window.monaco.editor.getEditors().at(-1);
                const model = editor.getModel();
                const offset = model.getValue().indexOf(token);
                if (offset < 0) throw new Error('insert target missing');
                const start = model.getPositionAt(offset);
                const end = model.getPositionAt(offset + token.length);
                editor.setSelection({startLineNumber: start.lineNumber, startColumn: start.column,
                    endLineNumber: end.lineNumber, endColumn: end.column});
            }""", token)

        owner, page = session(settings["owner"])
        try:
            expect.set_options(timeout=25000)
            page.goto(base, wait_until="domcontentloaded")
            choose_case(page)
            draft = formula(page)
            add = page.get_by_role("button", name="插入编辑器光标处")
            mode = page.get_by_role("combobox", name="公式包裹格式")
            preview = page.get_by_test_id("formula-preview")
            feedback = page.get_by_test_id("formula-insertion")
            expect(add).to_be_disabled()
            categories = page.get_by_role("group", name="符号分类")
            buttons = page.get_by_role("group", name="当前分类符号")
            for category, glyph, source in SYMBOLS:
                choice = categories.get_by_role("button", name=category, exact=True)
                choice.click()
                expect(choice).to_have_attribute("aria-pressed", "true")
                buttons.get_by_role("button", name=glyph, exact=True).click()
                result["categories"].append(category)
            picked_source = " ".join(source for _, _, source in SYMBOLS)
            expect(draft).to_have_value(picked_source)
            expect(preview.locator("annotation[encoding='application/x-tex']")).to_have_text(picked_source)
            assert preview.locator(".katex-html .mfrac").count() > 0 and preview.locator(".katex-html .mtable").count() > 0
            add.click()
            expect(feedback).to_contain_text("请先打开")
            expect(draft).to_have_value(picked_source)
            result["no_open_file_keeps_formula"] = True

            draft.fill(r"x < y + \sqrt{")
            diagnostic = page.get_by_test_id("formula-preview-error")
            expect(diagnostic).to_contain_text("KaTeX parse error")
            assert diagnostic.evaluate("node => node.children.length") == 0
            expect(preview).to_have_count(0)
            draft.fill(INLINE)
            expect(preview.locator("math")).to_have_count(1)
            expect(diagnostic).to_have_count(0)
            result["parse_error_is_text_and_can_be_corrected"] = True

            open_file(page, NOTES)
            page.get_by_test_id("markdown-preview-toggle").click()
            expect(page.get_by_test_id("markdown-preview")).to_contain_text("Formula notes")
            formula(page)
            add.click()
            expect(feedback).to_contain_text("源码视图")
            expect(draft).to_have_value(INLINE)
            result["markdown_preview_keeps_formula"] = True

            tab = open_file(page, TEX)
            formula(page)
            page.wait_for_function("text => window.monaco.editor.getEditors().at(-1)?.getModel()?.getValue() === text", arg=BASELINE)
            select_token(page, "INLINE_SLOT")
            add.click()
            expect(feedback).to_contain_text("已插入 formulas.tex")
            assert model_value(page) == BASELINE.replace("INLINE_SLOT", f"${INLINE}$")
            page.keyboard.press("Control+z")
            assert model_value(page) == BASELINE
            result["one_step_monaco_undo"] = True

            # Insert each wrapper at a selected range in the same actual file.
            for wrap, slot, source in (("inline", "INLINE_SLOT", INLINE), ("display", "DISPLAY_SLOT", DISPLAY),
                                       ("equation", "LABELED_SLOT", LABELED), ("equation", "EQUATION_SLOT", EQUATION)):
                mode.select_option(wrap)
                draft.fill(source)
                expect(preview.locator("annotation[encoding='application/x-tex']")).to_have_text(source)
                assert preview.locator(".katex-display").count() == (0 if wrap == "inline" else 1)
                select_token(page, slot)
                if wrap == "equation":
                    label = page.get_by_role("textbox", name="公式标签")
                    if slot == "LABELED_SLOT":
                        label.fill("eq:{pressure}")
                        before = model_value(page)
                        add.click()
                        expect(feedback).to_contain_text("标签不可包含")
                        expect(draft).to_have_value(source)
                        assert model_value(page) == before
                        label.fill("eq:pressure_1")
                        result["invalid_label_keeps_formula"] = True
                    else:
                        label.fill("")
                add.click()
                expect(feedback).to_contain_text("已插入 formulas.tex")
                expect(draft).to_have_value("")
            assert model_value(page) == EXPECTED
            result["wrappers"] = ["inline", "display", "equation_with_label", "equation_without_label"]
            expect(tab).to_have_attribute("data-dirty", "true")
            deadline = time.monotonic() + 30
            while True:
                with page.expect_response(lambda response: response.request.method == "PUT" and urlsplit(response.url).path == file_route) as saved:
                    page.get_by_role("button", name="保存 (Ctrl+S)", exact=True).click()
                response = saved.value
                meta = response.request.post_data_json
                assert meta["expected_sha256"] == sha(BASELINE) and meta["content"] == EXPECTED
                if response.status == 200:
                    break
                detail = response.json().get("detail") if response.status == 409 else None
                assert isinstance(detail, dict) and detail.get("code") == "storage_quota_busy", (response.status, response.text()[:200])
                assert time.monotonic() < deadline, "Storage did not settle in its observed retry window"
                expect(tab).to_have_attribute("data-dirty", "true")
                assert model_value(page) == EXPECTED
                result["quota_retries"] += 1
                page.wait_for_timeout(300)
            expect(tab).to_have_attribute("data-dirty", "false")
            result["saved_sha256"] = sha(EXPECTED)
            draft.fill(DISPLAY)
            mode.select_option("display")
            page.screenshot(path=str(root / "formula-editor.png"))

            page.reload(wait_until="domcontentloaded")
            choose_case(page)
            open_file(page, TEX)
            page.wait_for_function("text => window.monaco.editor.getEditors().at(-1)?.getModel()?.getValue() === text", arg=EXPECTED)
            result["saved_file_after_browser_reload"] = True

            viewer, read_page = session(settings["viewer"])
            try:
                read_page.goto(base, wait_until="domcontentloaded")
                choose_case(read_page)
                shared = open_file(read_page, TEX)
                read_draft = formula(read_page)
                read_draft.fill(EQUATION)
                expect(read_page.get_by_test_id("formula-preview").locator("math")).to_have_count(1)
                read_page.get_by_role("button", name="插入编辑器光标处").click()
                expect(read_page.get_by_test_id("formula-insertion")).to_contain_text("只读")
                expect(read_draft).to_have_value(EQUATION)
                expect(shared).to_have_attribute("data-dirty", "false")
                assert model_value(read_page) == EXPECTED
                result["readonly_workspace_does_not_change_editor"] = True
            finally:
                viewer.close()
            assert result["unexpected_model_requests"] == 0
            result["passed"] = True
        finally:
            (root / "formula-browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            if not result["passed"]:
                page.screenshot(path=str(root / "formula-browser-failure.png"))
            owner.close()
            edge.close()


def verify(args):
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    api = IsolatedServer("e05-formula-palette", app="formula_palette_fixture:app")
    (api.root / "formula_palette_fixture.py").write_text("from app.main import app\n"
        "from fastapi.staticfiles import StaticFiles\n"
        "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n", encoding="utf8")
    browser_tmp = api.root / "browser-tmp"
    browser_tmp.mkdir()
    print("E05_FORMULA_EVIDENCE", api.root, flush=True)
    report = {"passed": False, "model_calls": 0, "database": "sqlite", "storage": "fs"}
    try:
        with api:
            api.create_case(CASE)
            workspace = api.call("GET", api.prefix)["workspace_id"]
            api.put(TEX, BASELINE)
            api.put(NOTES, NOTE_TEXT)
            guest_email = "e05-formula-reader@example.com"
            reader = api.call("POST", "/auth/register", {"email": guest_email, "name": "Formula reader",
                                                          "password": "e05-isolated-reader-123"})["token"]
            guest = {"Authorization": "Bearer " + reader}
            tex_source = api.prefix + "/file?" + urlencode({"path": TEX})
            api.call("GET", tex_source, extra_headers=guest, expect=404)
            org = api.call("GET", "/org/mine")["org"]["id"]
            api.call("POST", f"/org/{org}/members", {"email": guest_email, "role": "viewer"}, expect=201)
            api.call("POST", f"/org/{org}/invitation:accept", {}, extra_headers=guest)
            api.call("PUT", f"/workspaces/{workspace}/sharing", {"org_id": org, "role": "viewer"})
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps({"root": str(api.root), "base": api.base, "prefix": api.prefix,
                    "owner": api.token, "viewer": reader, "workspace": workspace, "channel": args.channel}),
                text=True, check=True, timeout=220, env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
            report["browser"] = json.loads((api.root / "formula-browser.json").read_text("utf8"))
            assert report["browser"]["passed"]
            assert api.call("GET", tex_source, extra_headers=guest)["content"] == EXPECTED
            api.call("PUT", api.prefix + "/file", {"path": TEX, "content": "different", "expected_sha256": sha(EXPECTED)},
                     extra_headers=guest, expect=404)
            readback = api.call("GET", tex_source)
            raw = api.call("GET", api.prefix + "/raw?" + urlencode({"path": TEX}), raw=True)
            assert readback["content"] == EXPECTED and readback["content_sha256"] == sha(EXPECTED)
            assert raw == EXPECTED.encode("utf8")
            assert api.call("GET", api.prefix + "/file?" + urlencode({"path": NOTES}))["content"] == NOTE_TEXT
            report["verified_file_sha256"] = sha(EXPECTED)
            report["acl"] = True
        report["passed"] = api.proc.poll() is not None
    finally:
        report["api_exited"] = api.proc is not None and api.proc.poll() is not None
        (api.root / "formula-palette-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    assert report["passed"]
    print("E05_FORMULA_PASS", api.root / "formula-palette-result.json", flush=True)


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
