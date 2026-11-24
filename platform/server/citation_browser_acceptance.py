"""Live scholarly providers and real browser citation insertion on isolated files."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit


CASE = "E06 citation acceptance"
TEX = "E-report/latex/citations.tex"
BIB = "E-report/latex/refs.bib"
CROSSREF = "10.1113/jphysiol.1952.sp004764"
ARXIV = "1706.03762"
TEXT = "Local LOCAL_SLOT\nCrossref CROSS_SLOT\narXiv ARXIV_SLOT\nRepeated DUP_SLOT\n"
BIB_BASE = """% Keep the original comment and entries.
@book{localRef,
  title = {Engineering Notes},
  year = {2001}
}

@article{hodgkin1952,
  title = {A distinct work with the same candidate key},
  doi = {10.5555/e06-other-record}
}
"""
COMPETING = BIB_BASE + "% Concurrent editor change.\n"


def digest(value):
    return sha256(value.encode("utf8")).hexdigest()


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    base, root = settings["base"], Path(settings["root"])
    api_prefix = "/api/v1" + settings["prefix"]
    file_url = base + api_prefix + "/file"
    headers = {"Authorization": "Bearer " + settings["token"]}
    result = {"passed": False, "model_calls": 0, "quota_retries": 0}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings["channel"])
        context = edge.new_context(viewport={"width": 1680, "height": 970}, locale="zh-CN")
        context.add_init_script("if(location.origin===" + json.dumps(base) + ")localStorage.setItem('vf_token',"
                                + json.dumps(settings["token"]) + ");")
        page = context.new_page()
        page.set_default_timeout(35000)
        bib_writes = []

        def observe(request):
            if request.method == "POST" and re.match(r"/api/v1/(?:llm|chat|agents?)(?:/|:)", urlsplit(request.url).path):
                result["model_calls"] += 1
            if request.method == "PUT" and request.url == file_url and request.post_data_json.get("path") == BIB:
                bib_writes.append(request.post_data_json)

        def snapshot(path):
            response = context.request.get(file_url + "?" + urlencode({"path": path}), headers=headers)
            assert response.status == 200, (response.status, response.text()[:200])
            return response.json()

        def quota_busy(response):
            detail = response.json().get("detail") if response.status == 409 else None
            return isinstance(detail, dict) and detail.get("code") == "storage_quota_busy"

        def normal_write(button, path):
            deadline = time.monotonic() + 25
            while True:
                with page.expect_response(lambda response: response.request.method == "PUT" and response.url == file_url
                                          and response.request.post_data_json.get("path") == path) as sent:
                    button.click()
                response = sent.value
                if not quota_busy(response):
                    assert response.status == 200, (response.status, response.text()[:200])
                    return response.request.post_data_json
                assert time.monotonic() < deadline
                result["quota_retries"] += 1
                page.wait_for_timeout(320)

        def select(name):
            page.evaluate("""name => {
              const editor = window.monaco.editor.getEditors().at(-1);
              const model = editor.getModel();
              const offset = model.getValue().indexOf(name);
              if (offset < 0) throw new Error('missing citation target');
              const start = model.getPositionAt(offset), end = model.getPositionAt(offset + name.length);
              editor.setSelection({startLineNumber:start.lineNumber,startColumn:start.column,
                endLineNumber:end.lineNumber,endColumn:end.column});
            }""", name)

        def text():
            return page.evaluate("window.monaco.editor.getEditors().at(-1)?.getValue()")

        def search(source, query, status=200):
            page.get_by_test_id("cite-source").select_option(source)
            page.get_by_test_id("cite-query").fill(query)
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/v1/cite/search") as found:
                page.get_by_test_id("cite-search-run").click()
            assert found.value.status in ((status,) if isinstance(status, int) else status), (found.value.status, found.value.text()[:200])
            return found.value

        def item_by_id(identifier):
            return page.locator('[data-citation-id=' + json.dumps(identifier) + ']')

        try:
            page.on("request", observe)
            expect.set_options(timeout=35000)
            page.goto(base, wait_until="domcontentloaded")
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^公式调色板$")).click()
            local = page.get_by_test_id("cite-local-item").filter(has_text="localRef").get_by_role("button")
            feedback = page.get_by_test_id("cite-feedback")
            expect(local).to_be_visible()
            crossed = search("crossref", CROSSREF).json()["items"]
            paper = next(item for item in crossed if item["id"].lower() == "doi:" + CROSSREF)
            assert paper["source"] == "crossref" and paper["key_hint"] == "hodgkin1952"
            cross_row = item_by_id(paper["id"])
            cross_button = cross_row.get_by_test_id("cite-add")
            cross_button.click()
            expect(feedback).to_contain_text("请先打开")
            assert not bib_writes and snapshot(BIB)["content"] == BIB_BASE
            result["missing_editor_preserved_library"] = True

            for name in ("E-report", "E-report/latex"):
                folder = page.locator('[data-path=' + json.dumps(name) + ']')
                if folder.get_attribute("aria-expanded") == "false":
                    folder.click()
            page.locator('[data-path=' + json.dumps(TEX) + ']').click()
            page.wait_for_function("text => window.monaco.editor.getEditors().at(-1)?.getValue() === text", arg=TEXT)
            select("LOCAL_SLOT")
            local.click()
            expect(feedback).to_contain_text("已插入 localRef")
            local_text = TEXT.replace("LOCAL_SLOT", r"\cite{localRef}")
            assert text() == local_text and not bib_writes

            def competing_edit(route):
                request = route.request
                assert request.method == "PUT" and request.post_data_json["path"] == BIB
                assert request.post_data_json["expected_sha256"] == digest(BIB_BASE)
                deadline = time.monotonic() + 20
                while True:
                    competing = context.request.put(file_url, headers=headers,
                        data={"path": BIB, "content": COMPETING, "expected_sha256": digest(BIB_BASE)})
                    if not quota_busy(competing):
                        break
                    assert time.monotonic() < deadline
                    time.sleep(.4)
                assert competing.status == 200, (competing.status, competing.text()[:200])
                while True:
                    response = route.fetch()
                    if not quota_busy(response):
                        break
                    assert time.monotonic() < deadline
                    time.sleep(.4)
                detail = response.json().get("detail")
                assert response.status == 409 and detail.get("code") == "file_conflict", (response.status, detail)
                route.fulfill(response=response)

            select("CROSS_SLOT")
            page.route(file_url, competing_edit, times=1)
            cross_button.click()
            expect(feedback).to_contain_text("引用未插入")
            assert text() == local_text and snapshot(BIB)["content"] == COMPETING
            result["conditional_conflict_preserved_bibliography_and_editor"] = True
            assert normal_write(cross_button, BIB)["expected_sha256"] == digest(COMPETING)
            expect(feedback).to_contain_text("已录入并插入 hodgkin1952-2")
            expect(page.get_by_test_id("cite-local-item").filter(has_text="hodgkin1952-2")).to_have_count(1)
            current = snapshot(BIB)["content"]
            assert current.startswith(COMPETING) and current.count("{" + CROSSREF + "}") == 1
            result["crossref"] = {"id": paper["id"], "key": "hodgkin1952-2"}
            cross_text = local_text.replace("CROSS_SLOT", r"\cite{hodgkin1952-2}")
            assert text() == cross_text

            previous_writes = len(bib_writes)
            select("DUP_SLOT")
            cross_button.click()
            expect(feedback).to_contain_text("已录入并插入 hodgkin1952-2")
            expected = cross_text.replace("DUP_SLOT", r"\cite{hodgkin1952-2}")
            page.wait_for_function("value => window.monaco.editor.getEditors().at(-1)?.getValue() === value", arg=expected)
            assert len(bib_writes) == previous_writes and snapshot(BIB)["content"] == current
            result["repeat_did_not_append_duplicate"] = True

            keyword = search("arxiv", "Attention Is All You Need", status=(200, 502, 503))
            result["arxiv_keyword"] = {"http": keyword.status}
            if keyword.ok:
                result["arxiv_keyword"]["records"] = len(keyword.json()["items"])
            else:
                result["arxiv_keyword"]["detail"] = keyword.json()["detail"]
                expect(page.get_by_test_id("cite-error")).to_contain_text("文献检索失败")
                assert snapshot(BIB)["content"] == current and text() == expected
            page.wait_for_timeout(3150)  # arXiv documents a three-second interval between requests.
            searched = search("arxiv", ARXIV).json()["items"]
            arxiv = next(item for item in searched if re.fullmatch(r"arXiv:1706\.03762(?:v\d+)?", item["id"]))
            assert arxiv["source"] == "arxiv"
            select("ARXIV_SLOT")
            assert normal_write(item_by_id(arxiv["id"]).get_by_test_id("cite-add"), BIB)["expected_sha256"] == digest(current)
            expect(feedback).to_contain_text(f"已录入并插入 {arxiv['key_hint']}")
            expected = expected.replace("ARXIV_SLOT", "\\cite{" + arxiv["key_hint"] + "}")
            assert text() == expected
            final_bib = snapshot(BIB)["content"]
            assert final_bib.startswith(current) and final_bib.count("{" + arxiv["id"][6:] + "}") == 1
            result["arxiv"] = {"id": arxiv["id"], "key": arxiv["key_hint"]}
            written = normal_write(page.get_by_role("button", name="保存 (Ctrl+S)", exact=True), TEX)
            assert written["expected_sha256"] == digest(TEXT) and written["content"] == expected
            assert snapshot(TEX)["content"] == expected
            assert result["model_calls"] == 0
            result["source_sha256"] = digest(expected)
            result["refs_sha256"] = digest(final_bib)
            page.screenshot(path=str(root / "citation-workbench.png"))
            result["passed"] = True
        finally:
            (root / "citation-browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            if not result["passed"]:
                page.screenshot(path=str(root / "citation-failure.png"))
            edge.close()


def verify(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("e06-citation", app="citation_fixture:app")
    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    (api.root / "citation_fixture.py").write_text("from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n", encoding="utf8")
    browser_tmp = api.root / "browser-tmp"
    browser_tmp.mkdir()
    report = {"passed": False, "model_calls": 0, "storage": "fs", "database": "sqlite"}
    print("E06_CITATION_EVIDENCE", api.root, flush=True)
    try:
        with api:
            api.call("GET", "/cite/search?" + urlencode({"q": CROSSREF}), expect=401)
            api.create_case(CASE)
            api.put(TEX, TEXT)
            api.put(BIB, BIB_BASE)
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps({"root": str(api.root), "base": api.base, "prefix": api.prefix,
                                  "token": api.token, "channel": args.channel}), text=True, check=True, timeout=260,
                env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
            result = json.loads((api.root / "citation-browser.json").read_text("utf8"))
            assert result["passed"]
            for path, key in ((BIB, "refs_sha256"), (TEX, "source_sha256")):
                raw = api.call("GET", api.prefix + "/raw?" + urlencode({"path": path}), raw=True)
                assert sha256(raw).hexdigest() == result[key]
            guest = api.call("POST", "/auth/register", {"email": "e06-outsider@example.com", "name": "Citation outsider",
                             "password": "e06-isolated-account-123"})["token"]
            guest_headers = {"Authorization": "Bearer " + guest}
            api.call("GET", api.prefix + "/file?" + urlencode({"path": BIB}), expect=404, extra_headers=guest_headers)
            api.call("PUT", api.prefix + "/file", {"path": BIB, "content": "different", "expected_sha256": result["refs_sha256"]},
                     expect=404, extra_headers=guest_headers)
            report["browser"] = result
            report["raw_file_readback_and_acl"] = True
        report["passed"] = api.proc.poll() is not None
    finally:
        report["api_exited"] = api.proc is not None and api.proc.poll() is not None
        (api.root / "citation-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    assert report["passed"]
    print("E06_CITATION_PASS", api.root / "citation-result.json", flush=True)


def probe_keywords(queries):
    from urllib.error import HTTPError
    from urllib.request import Request
    from testsupport import IsolatedServer

    api = IsolatedServer("e06-arxiv-keywords", access_log=True)
    results = []
    with api:
        api.create_case("E06 public keyword health")
        for index, query in enumerate(queries):
            if index:
                time.sleep(3.2)
            path = "/api/v1/cite/search?" + urlencode({"source": "arxiv", "q": query})
            request = Request(api.base + path, headers={"Authorization": "Bearer " + api.token})
            try:
                response = api.opener.open(request, timeout=25)
            except HTTPError as error:
                response = error
            with response:
                status, data = response.code, json.load(response)
            assert status in {200, 502, 503}, (status, data)
            item = {"query": query, "http": status}
            if status == 200:
                items = data["items"]
                assert all(record["source"] == "arxiv" and record["id"].startswith("arXiv:") and record["title"]
                           and record["bibtex"].startswith("@misc{") for record in items)
                item.update(count=len(items), first={"id": items[0]["id"], "title": items[0]["title"]} if items else None)
            else:
                item["detail"] = data["detail"]
            results.append(item)
    (api.root / "keyword-results.json").write_text(json.dumps({"requests": results,
        "api_exited": api.proc.poll() is not None, "model_calls": 0}, ensure_ascii=False, indent=2), encoding="utf8")
    print("E06_ARXIV_KEYWORDS", api.root / "keyword-results.json", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dist", type=Path)
        parser.add_argument("--browser-python", type=Path)
        parser.add_argument("--channel", default="msedge")
        parser.add_argument("--arxiv-keyword", action="append", metavar="QUERY")
        args = parser.parse_args()
        if args.arxiv_keyword:
            if len(args.arxiv_keyword) > 2 or args.dist or args.browser_python:
                parser.error("keyword probe takes at most two queries without browser options")
            probe_keywords(args.arxiv_keyword)
        else:
            if not args.dist or not args.browser_python:
                parser.error("browser acceptance requires --dist and --browser-python")
            verify(args)
