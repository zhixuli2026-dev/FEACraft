"""Verify original and current editor auto-preview behavior with real CadQuery and Edge."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit


FIRST = "Auto preview primary"
OTHER = "Auto preview alternate"
GEOMETRY = "A-geo/geometry.py"
NOTE = "A-geo/preview-note.txt"
INITIAL = 'import cadquery as cq\ndef build_geometry(ctx):\n    return {"first_solid": cq.Workplane("XY").box(1, 2, 3)}\n'
FAILURE = 'import time\ntime.sleep(5)\nraise RuntimeError("B05_AUTOPREVIEW_NATIVE_FAILURE")\n'
RECOVERED = 'import cadquery as cq\ndef build_geometry(ctx):\n    return {"repaired_solid": cq.Workplane("XY").box(2, 3, 4)}\n'


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    base, root = settings["base"], Path(settings["root"])
    prefix = f'/api/v1/cases/{settings["case_id"]}'
    headers = {"Authorization": "Bearer " + settings["token"]}
    observed = {"phases": {}}
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(channel=settings["channel"])
        context = await chromium.new_context(viewport={"width": 1450, "height": 950})
        await context.add_init_script('if(location.origin===' + json.dumps(base) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings["token"]) + ');')
        page = await context.new_page()
        page.set_default_timeout(20000)
        expect.set_options(timeout=25000)
        toolbar = page.locator("[data-auto-job]")
        status = page.get_by_test_id("geo-status")
        info = page.get_by_test_id("geo-obj-info")
        editors = page.get_by_test_id("open-editors")
        posted = []

        def observe(request):
            if request.method == "POST" and re.fullmatch(r"/api/v1/cases/\d+/geometry:preview", urlsplit(request.url).path):
                posted.append(request.url)

        def evidence():
            (root / "autopreview-browser.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf8")

        page.on("request", observe)

        async def select(name):
            chosen = page.locator("[data-case-name=" + json.dumps(name) + "]")
            await chosen.click()
            await expect(chosen).to_have_attribute("aria-pressed", "true")

        async def geometry_panel():
            if not await toolbar.is_visible():
                await page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^几何预览$")).click()
            await expect(toolbar).to_be_visible()

        async def edit(path, previous, next_value):
            row = page.locator(f'[data-path="{path}"]')
            if not await row.is_visible():
                folder = page.locator(f'[data-path="{path.split("/")[0]}"]')
                await expect(folder).to_be_visible()
                if await folder.get_attribute("aria-expanded") == "false":
                    await folder.click()
            await row.click()
            editor = page.locator(f'[data-editor-path="{path}"]')
            await expect(editor).to_have_attribute("data-active", "true")
            await page.wait_for_function('(text) => window.monaco?.editor.getModels().at(-1)?.getValue() === text', arg=previous)
            await page.evaluate('(text) => window.monaco.editor.getModels().at(-1).setValue(text)', next_value)
            await expect(editor).to_have_attribute("data-dirty", "true")

        async def save(button):
            async with page.expect_response(lambda response: urlsplit(response.url).path == prefix + "/file"
                and response.request.method == "PUT" and response.request.post_data_json["path"] == GEOMETRY) as received:
                await page.get_by_role("button", name=button, exact=True).click()
            response = await received.value
            assert response.status == 200, await response.text()
            return (await response.json())["auto_preview_job"]

        async def request(path):
            response = await context.request.get(base + path, headers=headers)
            assert response.status == 200, await response.text()
            return await response.json()

        async def terminal(identifier, expected):
            until = time.monotonic() + 45
            while time.monotonic() < until:
                job = await request(f"/api/v1/jobs/{identifier}")
                if job["status"] not in ("queued", "running"):
                    assert job["status"] == expected, job["log_backlog"]
                    assert job["attempt"]["process_stop"]["verified"]
                    return job
                await asyncio.sleep(.1)
            raise TimeoutError(f"Native preview {identifier} did not reach {expected}")

        await page.goto(base)
        await select(FIRST)
        await edit(GEOMETRY, settings["original_source"], INITIAL)
        first = await save("保存 (Ctrl+S)")
        await geometry_panel()
        await expect(info).to_contain_text("first_solid")
        assert (await terminal(first, "succeeded"))["exit_code"] == 0
        scene = await request(prefix + "/geometry/scene")
        publication = scene["publication"]["id"]
        observed["phases"]["single_save_preview"] = {"job_id": first, "publication": publication}
        evidence()

        await edit(GEOMETRY, INITIAL, FAILURE)
        await edit(NOTE, "Original note.", "Written in the same Save All action.")
        await expect(editors).to_have_attribute("data-dirty-count", "2")
        failed = await save("全部保存")
        assert failed != first
        await expect(editors).to_have_attribute("data-dirty-count", "0")
        note = await request(prefix + "/file?" + urlencode({"path": NOTE}))
        assert note["content"] == "Written in the same Save All action."
        await geometry_panel()

        if settings["observe_baseline"]:
            job = await terminal(failed, "failed")
            assert "B05_AUTOPREVIEW_NATIVE_FAILURE" in job["log_backlog"] and job["exit_code"] != 0
            assert await toolbar.get_attribute("data-auto-job") != str(failed)
            assert f"#{failed}" not in await status.inner_text()
            await expect(info).to_contain_text("first_solid")
            observed["phases"]["save_all_old_successful_write_untracked_failure"] = {
                "job_id": failed, "actual_exit_code": job["exit_code"],
                "toolbar_job_id": await toolbar.get_attribute("data-auto-job"),
                "toolbar_message": await status.inner_text()}
            evidence()
            await chromium.close()
            return

        await expect(toolbar).to_have_attribute("data-auto-job", str(failed))
        await select(OTHER)
        await geometry_panel()
        await expect(toolbar).to_have_attribute("data-auto-job", "")
        await expect(info).not_to_contain_text("first_solid")
        job = await terminal(failed, "failed")
        assert "B05_AUTOPREVIEW_NATIVE_FAILURE" in job["log_backlog"] and job["exit_code"] != 0
        await expect(status).not_to_contain_text(f"#{failed}")

        await select(FIRST)
        await geometry_panel()
        await expect(toolbar).to_have_attribute("data-auto-job", str(failed))
        await expect(status).to_contain_text(f"预览失败（作业 #{failed}），保留上一有效场景")
        await expect(info).to_contain_text("first_solid")
        retained = await request(prefix + "/geometry/scene")
        assert retained["publication"]["id"] == publication and retained["publication"]["stale"]
        assert retained["objects"] == scene["objects"]
        observed["phases"]["save_all_and_case_return_preserved_failure"] = {"job_id": failed,
            "actual_exit_code": job["exit_code"], "publication": retained["publication"]["id"],
            "same_objects": True, "alternate_case_kept_separate": True}
        await status.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / "autopreview-failure.png"))
        evidence()

        await edit(GEOMETRY, FAILURE, RECOVERED)
        repaired = await save("保存 (Ctrl+S)")
        await geometry_panel()
        assert (await terminal(repaired, "succeeded"))["exit_code"] == 0
        await expect(info).to_contain_text("repaired_solid")
        await expect(status).not_to_contain_text("预览失败")
        await expect(toolbar).to_have_attribute("data-auto-job", str(repaired))
        after = await request(prefix + "/geometry/scene")
        assert after["publication"]["id"] != publication and not after["publication"]["stale"]
        validation = await request(prefix + "/geometry/validation")
        assert validation["publication"]["id"] == after["publication"]["id"]
        volume = validation["objects"][0]["volume"]
        assert math.isclose(volume, 24, rel_tol=1e-12, abs_tol=1e-12), volume
        assert not posted, posted
        observed["phases"]["repaired_save_refreshed_without_manual_preview"] = {
            "job_id": repaired, "publication": after["publication"]["id"], "native_volume": volume}
        await info.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / "autopreview-repaired.png"))
        evidence()
        await chromium.close()


def verify(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-autopreview-browser", app="b05_fixture:app")
    bundle = args.dist.resolve(strict=True)
    (api.root / "b05_fixture.py").write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding="utf8")
    temp = api.root / "browser-tmp"
    temp.mkdir()
    result = {"passed": False, "model_calls": 0, "backend": "fs/sqlite", "observe_baseline": args.observe_baseline}
    try:
        with api:
            case = api.create_case(FIRST)
            prefix, token = api.prefix, api.token
            workspace = api.call("GET", prefix)["workspace_id"]
            api.call("POST", f"/workspaces/{workspace}/cases", {"name": OTHER})
            original = api.call("GET", prefix + "/file?" + urlencode({"path": GEOMETRY}))["content"]
            api.put(NOTE, "Original note.")
            settings = {"base": api.base, "root": str(api.root), "case_id": case["id"], "token": token,
                        "original_source": original, "channel": args.channel, "observe_baseline": args.observe_baseline}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=175,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            result["browser"] = json.loads((api.root / "autopreview-browser.json").read_text(encoding="utf8"))
            if not args.observe_baseline:
                repaired = result["browser"]["phases"]["repaired_save_refreshed_without_manual_preview"]
                api.create_case("Unrelated preview owner")
                api.call("GET", f'/jobs/{repaired["job_id"]}', expect=404)
                api.call("GET", prefix + "/geometry/scene", expect=404)
                result["cross_account_404"] = 2
        result["passed"] = True
    finally:
        (api.root / "geometry-autopreview-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print("GEOMETRY_AUTOPREVIEW_BROWSER_OK", api.root, flush=True)


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
        parser.add_argument("--observe-baseline", action="store_true")
        verify(parser.parse_args())
