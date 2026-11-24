"""Real Edge error capture and isolated admin report verification."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from urllib.parse import urlsplit


REPORT = "/api/v1/runtime/client-error"
READ = REPORT + "s"
BOUNDARY = "G11_REACT_BOUNDARY"
WINDOW = "G11_WINDOW_EXCEPTION"
REJECTION = "G11_PROMISE_REJECTION"
LEGACY = "G11_HISTORICAL_REPORT"
DIRECT = "G11_DIRECT_REPORT"
PAGE_CONTEXT = "G11_FIXTURE_CASE_CONTEXT"
PAGE_CODE = "G11_FIXTURE_OAUTH_CODE"
PAGE_FRAGMENT = "G11_FIXTURE_FRAGMENT"
LEGACY_CODE = "G11_FIXTURE_HISTORICAL_QUERY"
LEGACY_FRAGMENT = "G11_FIXTURE_HISTORICAL_FRAGMENT"
DIRECT_CODE = "G11_FIXTURE_DIRECT_QUERY"
DIRECT_FRAGMENT = "G11_FIXTURE_DIRECT_FRAGMENT"
DIRECT_USERINFO = "G11_FIXTURE_DIRECT_USERINFO"
AUTH_SENTINEL = "g11-only-fake-token"


BOUNDARY_FAULT = r"""
const owner = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "dataset");
Object.defineProperty(HTMLElement.prototype, "dataset", {
  ...owner,
  get() {
    const value = owner.get.call(this);
    if (this !== document.documentElement) return value;
    return new Proxy(value, {
      set(target, property, value) {
        if (property === "vfTheme") {
          Object.defineProperty(HTMLElement.prototype, "dataset", owner);
          throw new Error("G11_REACT_BOUNDARY " + location.href);
        }
        return Reflect.set(target, property, value);
      }
    });
  }
});
"""


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    base = settings["base"]
    root = Path(settings["root"])
    headers = {"Authorization": "Bearer " + settings["admin"]}
    page_url = base + f"/?case={PAGE_CONTEXT}&code={PAGE_CODE}#{PAGE_FRAGMENT}"
    private = (PAGE_CONTEXT, PAGE_CODE, PAGE_FRAGMENT)
    outbound = []
    statuses = []
    evidence = {}
    expect.set_options(timeout=25000)

    async with async_playwright() as playwright:
        edge = await playwright.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1440, "height": 940})
        await context.add_init_script('if(location.origin===' + json.dumps(base)
            + ')localStorage.setItem("vf_token",' + json.dumps(settings["admin"]) + ');')
        await context.add_init_script(BOUNDARY_FAULT)
        page = await context.new_page()

        def observe(request):
            if request.method == "POST" and urlsplit(request.url).path == REPORT:
                outbound.append(request)

        def status(response):
            if response.request.method == "POST" and urlsplit(response.url).path == REPORT:
                statuses.append(response.status)

        page.on("request", observe)
        page.on("response", status)

        async def admin_report():
            response = await context.request.get(base + READ + "?limit=15", headers=headers)
            assert response.status == 200, response.status
            return (await response.json())["items"]

        async def until_counts(expected):
            for _ in range(70):
                reports = await admin_report()
                actual = {kind: sum(item["message"].startswith(kind) for item in reports) for kind in expected}
                if actual == expected:
                    return reports
                await asyncio.sleep(.1)
            raise AssertionError(f"Unexpected report counts: {actual}, expected {expected}")

        async def confirmed_requests(count):
            for _ in range(30):
                if len(outbound) >= count and len(statuses) >= count:
                    break
                await asyncio.sleep(.05)
            assert len(outbound) == count and statuses == [200] * count, (len(outbound), statuses)
            return [{"payload": request.post_data_json, "referrer": await request.header_value("referer")}
                    for request in outbound]

        await page.goto(page_url)
        await expect(page.locator('[data-display-generation="1"]')).to_have_count(1)
        await expect(page.get_by_test_id("workspace-share")).to_be_visible()
        await until_counts({BOUNDARY: 1})

        await page.evaluate(
            """([windowMark, rejectionMark, fixtureToken]) => {
              window.g11WindowError = new Error(windowMark + " " + location.href);
              const fail = () => { throw window.g11WindowError; };
              setTimeout(fail, 0);
              setTimeout(fail, 15);
              Promise.reject(new Error(rejectionMark + " Bearer " + fixtureToken));
            }""",
            [WINDOW, REJECTION, AUTH_SENTINEL],
        )
        reports = await until_counts({BOUNDARY: 1, WINDOW: 1, REJECTION: 1})
        first_requests = await confirmed_requests(3)
        assert all(entry["payload"]["url"] == base + "/" and entry["referrer"] is None for entry in first_requests)
        assert not any(separate in json.dumps(first_requests) for separate in private)
        first = {kind: next(item for item in reports if item["message"].startswith(kind))
                 for kind in (BOUNDARY, WINDOW, REJECTION)}
        for kind, item in first.items():
            assert item["schema"] == "vibeflow.client-error.v1"
            assert item["url"] == base + "/" and item["stack"], kind
        assert "Bearer ***" in first[REJECTION]["message"]
        assert AUTH_SENTINEL not in json.dumps(first)

        await asyncio.sleep(5.05)
        await page.evaluate("() => { setTimeout(() => { throw window.g11WindowError; }, 0); }")
        reports = await until_counts({BOUNDARY: 1, WINDOW: 2, REJECTION: 1})
        all_requests = await confirmed_requests(4)
        assert {item["message"] for item in reports if item["message"].startswith(WINDOW)} == {WINDOW + " " + base + "/"}
        assert not any(separate in json.dumps(all_requests) for separate in private)
        evidence["edge"] = {
            "react_recovered_generation": 1,
            "exception_reports_within_window": 1,
            "exception_reports_after_five_seconds": 2,
            "promise_rejection_reports": 1,
            "authenticated_report_response_codes": statuses,
            "browser_payload_removed_page_query_and_fragment": True,
            "report_request_used_no_referrer": all(entry["referrer"] is None for entry in all_requests),
            "reports_keep_origin_path_and_stack": all(item["stack"] for item in first.values()),
        }
        (root / "browser-client-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf8")
        await edge.close()


def register(api, name):
    return api.call("POST", "/auth/register", {
        "email": "g11-" + uuid.uuid4().hex + "@example.com",
        "name": name, "password": "g11-isolated-test-password",
    })


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("client-error-browser", app="g11_fixture:app")
    root = api.root
    log = root / "client-errors.jsonl"
    mounted = Path(args.dist).resolve(strict=True)
    (root / "g11_fixture.py").write_text(
        "import os\nos.environ['VIBEFLOW_CLIENT_ERROR_LOG'] = " + repr(str(log))
        + "\nfrom app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(mounted)) + ", html=True))\n",
        encoding="utf8")
    print("CLIENT_ERROR_BROWSER_EVIDENCE", root, flush=True)
    report = {"passed": False, "model_calls": 0, "backend": "fs/sqlite"}
    try:
        with api:
            admin = register(api, "Crash report admin")
            member = register(api, "Crash report member")
            with sqlite3.connect(root / "test.sqlite") as db:
                changed = db.execute("UPDATE memberships SET role=? WHERE user_id=?", ("member", member["user"]["id"]))
                assert changed.rowcount == 1

            legacy_url = api.base + f"/past?code={LEGACY_CODE}#{LEGACY_FRAGMENT}"
            legacy = {"schema": "vibeflow.client-error.v1", "id": "g11-historical-record", "created_at": 0.0,
                      "message": LEGACY + " " + legacy_url, "stack": "at old handler (" + legacy_url + ")",
                      "url": legacy_url, "user_agent": "G11 fixture"}
            old_line = (json.dumps(legacy) + "\n").encode("utf8")
            log.write_bytes(old_line)
            browser_args = {"base": api.base, "root": str(root), "admin": admin["token"], "channel": args.channel}
            temp = root / "browser-tmp"
            temp.mkdir()
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(browser_args), text=True, check=True, timeout=100,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            report.update(json.loads((root / "browser-client-errors.json").read_text(encoding="utf8")))

            api.token = member["token"]
            api.call("GET", READ, api_prefix=False, expect=403)
            api.token = None
            api.call("GET", READ, api_prefix=False, expect=401)
            api.call("POST", REPORT, {"message": "G11 anonymous"}, api_prefix=False, expect=401)
            api.token = admin["token"]
            api.call("POST", REPORT, {"message": "   "}, api_prefix=False, expect=400)
            direct_url = api.base.replace("://", f"://g11:{DIRECT_USERINFO}@", 1)
            direct_url += f"/direct?access_token={DIRECT_CODE}#{DIRECT_FRAGMENT}"
            receipt = api.call("POST", REPORT, {"message": DIRECT + " " + direct_url,
                "stack": "at fixture (" + direct_url + ") " + "X" * 9000, "url": direct_url}, api_prefix=False)
            recent = api.call("GET", READ + "?limit=15", api_prefix=False)["items"]
            assert len(recent) == 6
            assert recent[0]["id"] == receipt["id"] and recent[0]["url"] == api.base + "/direct"
            assert len(recent[0]["stack"]) == 8000
            assert recent[-1]["id"] == legacy["id"] and recent[-1]["url"] == api.base + "/past"
            assert api.call("GET", READ + "?limit=1", api_prefix=False)["items"] == recent[:1]
            redacted = json.dumps(recent)
            assert not any(mark in redacted for mark in (
                LEGACY_CODE, LEGACY_FRAGMENT, DIRECT_CODE, DIRECT_FRAGMENT, DIRECT_USERINFO, AUTH_SENTINEL,
                PAGE_CONTEXT, PAGE_CODE, PAGE_FRAGMENT))
            appended = log.read_bytes()
            assert appended.startswith(old_line) and len(appended.splitlines()) == 6
            new_records = appended[len(old_line):]
            assert not any(mark.encode() in new_records for mark in (
                DIRECT_CODE, DIRECT_FRAGMENT, DIRECT_USERINFO, AUTH_SENTINEL, PAGE_CONTEXT, PAGE_CODE, PAGE_FRAGMENT))
            report["admin"] = {"read": 200, "member_read": 403, "anonymous_read": 401, "anonymous_report": 401,
                "blank_report": 400, "append_only_rows": 6, "latest_limit_verified": True, "stack_limit": 8000,
                "new_log_source_redacted": True, "old_read_source_redacted": True,
                "existing_row_unchanged_sha256": hashlib.sha256(old_line).hexdigest()}
        report["passed"] = True
    finally:
        (root / "client-error-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print("CLIENT_ERROR_BROWSER_OK", root, flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from regression_llm_policy import enforce
        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dist", type=Path, required=True)
        parser.add_argument("--browser-python", type=Path, required=True)
        parser.add_argument("--channel", default="msedge")
        main(parser.parse_args())
