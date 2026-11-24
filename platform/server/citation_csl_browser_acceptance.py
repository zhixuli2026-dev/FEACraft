"""Exercise real offline Pandoc citation rendering in the Edge workbench."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit

from testsupport import IsolatedServer, SERVER

CASE = "CSL publisher and offline bibliography"
BIB_PATH = "E-report/latex/refs.bib"
DOI = "10.1000/182"
BIB = r"""@article{delacruz2022a,
  title={Scoped Research A},
  author={de la Cruz, Ana and Lovelace, Ada},
  year={2022},
  journal={Journal of Testing},
  volume={7},
  number={3},
  pages={103--119},
  doi={10.1000/182}
}
@article{delacruz2022b,
  title={Scoped Research B},
  author={de la Cruz, Ana and Lovelace, Ada},
  year={2022},
  journal={Journal of Testing},
  pages={170--180},
  url={https://arxiv.org/abs/2401.00001},
  eprint={2401.00001},
  archivePrefix={arXiv}
}
"""
NOTE = """<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="note">
  <info><title>VibeFlow acceptance note</title><id>https://example.org/styles/acceptance-note</id>
  <updated>2026-11-19T00:00:00Z</updated></info>
  <citation><layout suffix="."><text variable="title" font-style="italic"/></layout></citation>
  <bibliography><layout><text variable="title"/><text variable="DOI" prefix=" · "/></layout></bibliography>
</style>
"""

APP = """
import os
from pathlib import Path
import httpx
from fastapi.staticfiles import StaticFiles
from app.main import app

scratch = Path(__file__).parent / 'native-tmp'
os.environ.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
original = httpx.AsyncClient.send

async def disconnected_scholarly_services(self, request, *args, **kwargs):
    if request.url.host in {'api.crossref.org', 'export.arxiv.org', 'api.semanticscholar.org'}:
        with (scratch.parent / 'external-search-attempts.txt').open('a', encoding='utf8') as log:
            log.write(request.url.host + '\\n')
        raise httpx.ConnectError('Isolated scholarly service network interruption', request=request)
    return await original(self, request, *args, **kwargs)

httpx.AsyncClient.send = disconnected_scholarly_services
app.mount('/', StaticFiles(directory=dist, html=True))
"""


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    base, root = settings["base"], Path(settings["root"])
    identity = {"Authorization": "Bearer " + settings["token"]}
    endpoint = f"/api/v1/cite/cases/{settings['case']}/format"
    scholar_attempts = root / "external-search-attempts.txt"
    data = {"passed": False, "model_requests": [], "page_errors": [], "render_http": [], "checks": []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel="msedge")
        data["edge"] = edge.version
        context = edge.new_context(viewport={"width": 1590, "height": 1050}, locale="zh-CN")
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(37_000)
        expect.set_options(timeout=35_000)
        page.on("pageerror", lambda error: data["page_errors"].append(str(error)))
        page.on("request", lambda request: data["model_requests"].append(urlsplit(request.url).path)
                if request.method == "POST" and urlsplit(request.url).path.endswith(("/messages", "/turns", "/goal:run")) else None)

        def selection(kind):
            page.get_by_test_id("cite-style").select_option(kind)

        def render(button, status=200):
            with page.expect_response(lambda response: urlsplit(response.url).path == endpoint) as outcome:
                button.click()
            response = outcome.value
            data["render_http"].append(response.status)
            assert response.status == status, (response.status, response.text()[:400])
            return response.json()

        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("[data-case-name=" + json.dumps(CASE) + "]").click()
            page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^公式调色板$")).click()
            section = page.get_by_role("region", name="引用", exact=True)
            refs = section.get_by_test_id("cite-local-item")
            expect(refs).to_have_count(2)
            section.get_by_test_id("cite-query").fill("Offline scholarship check")
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/v1/cite/search") as blocked:
                section.get_by_test_id("cite-search-run").click()
            assert blocked.value.status == 502, blocked.value.text()
            expect(section.get_by_test_id("cite-error")).to_contain_text("Crossref")
            assert scholar_attempts.read_text(encoding="utf8").splitlines() == ["api.crossref.org"]
            data["checks"].append("search_network_failure_keeps_local_bibliography")

            entry = section.get_by_role("button", name="预览 delacruz2022a", exact=True)
            formatted = section.get_by_test_id("cite-formatted")
            render(section.get_by_role("button", name="预览书目", exact=True))
            expect(formatted.locator(".citation")).to_have_count(2)
            expect(formatted.locator(".citation").nth(0)).to_contain_text("2022a")
            expect(formatted.locator(".citation").nth(1)).to_contain_text("2022b")
            doi_link = formatted.locator(f'a[href="https://doi.org/{DOI}"]')
            expect(doi_link).to_be_visible()
            expect(doi_link).to_have_css("color", "rgb(125, 180, 255)")
            expect(formatted.locator('a[href="https://arxiv.org/abs/2401.00001"]')).to_be_visible()
            expect(formatted).to_contain_text("Lovelace, A.")
            data["apa"] = formatted.inner_text()
            selection("ieee")
            render(entry)
            expect(formatted.locator(".citation")).to_have_text("[1]")
            expect(formatted).to_contain_text("Journal of Testing")
            data["ieee"] = formatted.inner_text()
            selection("chicago")
            render(entry)
            expect(formatted.locator(".citation")).to_contain_text("2022")
            expect(formatted).to_contain_text("Ada Lovelace")
            data["chicago"] = formatted.inner_text()
            data["checks"].append("real_apa_ieee_chicago_author_date_doi_arxiv_and_year_disambiguation")

            selection("custom")
            style = section.get_by_label("CSL 样式文件", exact=True)
            parent = section.get_by_label("父样式文件（有依赖时）", exact=True)
            style.set_input_files(settings["nature"])
            render(entry)
            expect(formatted.locator(".citation sup")).to_have_text("1")
            assert section.evaluate("(panel) => panel.scrollWidth <= panel.clientWidth + 1")
            expect(formatted).to_contain_text(re.compile("Scoped Research A", re.I))
            data["nature"] = formatted.inner_text()

            style.set_input_files(settings["dependent"])
            render(entry, 422)
            expect(section.get_by_test_id("cite-format-error")).to_contain_text("父样式")
            expect(formatted).to_have_count(0)
            parent.set_input_files({"name": "mismatched.csl", "mimeType": "text/xml", "buffer": NOTE.encode()})
            render(entry, 422)
            expect(section.get_by_test_id("cite-format-error")).to_contain_text("不匹配")
            parent.set_input_files(settings["nature"])
            render(entry)
            expect(formatted.locator(".citation sup")).to_have_text("1")
            data["checks"].append("real_nature_independent_and_dependent_publisher_style_with_local_parent_recovery")
            section.screenshot(path=str(root / "publisher-citation-panel.png"))

            section.get_by_role("button", name="移除父样式", exact=True).click()
            style.set_input_files({"name": "broken.csl", "mimeType": "text/xml", "buffer": b"<style"})
            render(entry, 422)
            expect(section.get_by_test_id("cite-format-error")).to_contain_text("CSL XML")
            style.set_input_files({"name": "note.csl", "mimeType": "text/xml", "buffer": NOTE.encode()})
            render(entry)
            expect(formatted.locator("li em")).to_contain_text(re.compile("Scoped Research A", re.I))
            expect(formatted.locator('a[href="#fn1"]')).to_be_visible()
            data["checks"].append("invalid_xml_recovers_to_custom_note_style_with_formatting")

            payload = {"keys": ["nonexistent"], "bibtex": BIB, "preset": "apa"}
            missing = context.request.post(base + endpoint, data=payload, headers=identity)
            assert missing.status == 422, missing.text()
            assert context.request.post(base + endpoint, data=payload, headers={
                "Authorization": "Bearer " + settings["foreign"]}).status == 404
            file_url = f"{base}/api/v1/cases/{settings['case']}/file"
            source = context.request.get(file_url, params={"path": BIB_PATH}, headers=identity)
            assert source.ok and source.json()["content"] == BIB
            data["bibtex_sha256"] = sha256(BIB.encode()).hexdigest()
            assert scholar_attempts.read_text(encoding="utf8").splitlines() == ["api.crossref.org"]
            assert not data["model_requests"] and not data["page_errors"], data
            data["passed"] = True
        finally:
            if not data["passed"]:
                page.screenshot(path=str(root / "citation-csl-failure.png"))
            write_json(root / "citation-csl-browser.json", data)
            edge.close()


def verify(args):
    server = IsolatedServer("citation-csl-e08", app="citation_csl_fixture:app")
    pandoc = Path(args.pandoc).resolve(strict=True)
    root = server.root
    scratch = root / "native-tmp"
    scratch.mkdir()
    (root / "runtime.json").write_text(json.dumps({"pandoc": str(pandoc)}), encoding="utf8")
    (root / "citation_csl_fixture.py").write_text(
        "dist=" + repr(str(args.dist.resolve(strict=True))) + "\n" + APP, encoding="utf8")
    result = {"passed": False, "models": 0, "pandoc": subprocess.check_output([str(pandoc), "--version"],
              encoding="utf8").splitlines()[0]}
    print("CITATION_CSL_EVIDENCE", root, flush=True)
    try:
        with server:
            case = server.create_case(CASE)
            server.put(BIB_PATH, BIB)
            outsider = server.call("POST", "/auth/register", {"name": "Another reader",
                "email": "foreign-csl@example.com", "password": "isolated-test-123"})
            options = {"case": case["id"], "base": server.base, "root": str(root), "token": server.token,
                       "foreign": outsider["token"], "nature": str(args.nature.resolve(strict=True)),
                       "dependent": str(args.dependent.resolve(strict=True))}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", __file__, "--browser"],
                           input=json.dumps(options), text=True, encoding="utf8", check=True, timeout=200, cwd=root)
            result["browser"] = json.loads((root / "citation-csl-browser.json").read_text(encoding="utf8"))
            with sqlite3.connect((root / "test.sqlite").as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute("SELECT phase,children_json FROM native_requests WHERE kind='report.prepare' "
                                  "AND json_extract(profile_json, '$.purpose')='pandoc_cite'").fetchall()
            result["native_requests"] = len(rows)
            assert len(rows) >= 7
            for phase, raw in rows:
                children = json.loads(raw)["children"].values()
                assert phase == "succeeded" and len(children) == 1
                assert all(child["purpose"] == "pandoc_cite" and child["stop"]["verified"] for child in children)
            assert not list((root / "files/.citation-previews").glob(".pandoc-call-*"))
        result["api_exit"] = server.proc.poll()
        assert result["api_exit"] is not None
        result["passed"] = True
    except BaseException as error:
        result["failure"] = {"type": type(error).__name__, "message": str(error)[:850]}
        raise
    finally:
        write_json(root / "citation-csl-result.json", result)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(SERVER.parent / "tools"))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        cli = argparse.ArgumentParser(description=__doc__)
        for name in ("dist", "browser-python", "pandoc", "nature", "dependent"):
            cli.add_argument("--" + name, type=Path, required=True)
        verify(cli.parse_args())
