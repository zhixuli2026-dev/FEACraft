"""Check browser save acknowledgements across real SQLite/preview contention."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


CASE = "Saved geometry before preview"
OTHER = "Unrelated geometry handoff"
GEOMETRY = "A-geo/geometry.py"
NOTE = "A-geo/preview-note.txt"
ORIGINAL_NOTE = "Original note."
FINAL_NOTE = "Saved together with geometry."
QUOTA_WAIT = .25
HOLD_SECONDS = .65


def source(name, width):
    return ('import cadquery as cq\n'
            'def build_geometry(ctx):\n'
            f'    return {{{name!r}: cq.Workplane("XY").box({width}, 2, 3)}}\n')


def digest(content):
    return hashlib.sha256(content.encode("utf8")).hexdigest()


APP_SOURCE = '''
import asyncio
from contextvars import ContextVar
import hashlib
from pathlib import Path
import sqlite3
import time

from app.main import app
from app.services import file_transactions, storage_admission
from fastapi.staticfiles import StaticFiles

root = Path(__file__).parent
app.mount("/", StaticFiles(directory={bundle!r}, html=True))
finish_edit = file_transactions.finish_committed_edit
original_bounds = storage_admission.bounds
limit_handoff_wait = ContextVar("limit_handoff_wait", default=False)
writers = set()


def bounds(*, settlement=False):
    deadline, parent = original_bounds(settlement=settlement)
    if limit_handoff_wait.get() and not settlement:
        deadline = min(deadline, time.monotonic() + {quota_wait!r})
    return deadline, parent


def hold_writer(loop, ready, expected):
    with sqlite3.connect(root / "test.sqlite", timeout=5) as connection:
        connection.execute("BEGIN IMMEDIATE")
        (root / (expected + ".writer-started")).touch()
        loop.call_soon_threadsafe(ready.set)
        time.sleep({hold_seconds!r})
        connection.rollback()
    (root / (expected + ".writer-released")).touch()


async def after_commit(db, case, owner_id):
    await finish_edit(db, case, owner_id)
    trigger = root / "hold-after-geometry-save"
    if not trigger.is_file():
        return
    case_id, expected = trigger.read_text(encoding="utf8").split()
    if case.id != int(case_id):
        return
    geometry = root / "files" / case.s3_prefix / "A-geo/geometry.py"
    if hashlib.sha256(geometry.read_bytes()).hexdigest() != expected:
        return
    trigger.unlink()
    ready = asyncio.Event()
    writer = asyncio.create_task(asyncio.to_thread(
        hold_writer, asyncio.get_running_loop(), ready, expected))
    writers.add(writer)
    writer.add_done_callback(writers.discard)
    await asyncio.wait_for(ready.wait(), timeout=8)
    limit_handoff_wait.set(True)


file_transactions.finish_committed_edit = after_commit
storage_admission.bounds = bounds
'''


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings["root"])
    base, case_id = settings["base"], settings["case_id"]
    prefix = f"/api/v1/cases/{case_id}"
    auth = {"Authorization": "Bearer " + settings["token"]}
    evidence = {}

    def persist():
        (root / "browser-handoff.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf8")

    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1440, "height": 940})
        await context.add_init_script('if(location.origin===' + json.dumps(base)
            + ')localStorage.setItem("vf_token",' + json.dumps(settings["token"]) + ');')
        page = await context.new_page()
        page.set_default_timeout(20000)
        expect.set_options(timeout=25000)
        editor_warning = page.get_by_test_id("editor-auto-preview-warning")
        geometry_warning = page.get_by_test_id("geo-auto-preview-warning")
        editor_failed = page.get_by_test_id("editor-save-error")
        tabs = page.get_by_test_id("open-editors")
        object_info = page.get_by_test_id("geo-obj-info")

        async def get(path):
            response = await context.request.get(base + path, headers=auth)
            assert response.status == 200, await response.text()
            return await response.json()

        async def select_case(name):
            item = page.locator("[data-case-name=" + json.dumps(name) + "]")
            if await item.get_attribute("aria-pressed") != "true":
                await item.click()
            await expect(item).to_have_attribute("aria-pressed", "true")

        async def show_panel(title, locator):
            if not await locator.is_visible():
                await page.locator(".dv-tab:visible").filter(has_text=re.compile("^" + title + "$")).click()
            await expect(locator).to_be_visible()

        async def open_editor(path):
            row = page.locator(f'[data-path="{path}"]')
            if not await row.is_visible():
                folder = page.locator('[data-path="A-geo"]')
                await expect(folder).to_be_visible()
                if await folder.get_attribute("aria-expanded") == "false":
                    await folder.click()
            await row.click()
            tab = page.locator(f'[data-editor-path="{path}"]')
            await expect(tab).to_have_attribute("data-active", "true")
            return tab

        async def edit(path, previous, next_value):
            tab = await open_editor(path)
            await page.wait_for_function(
                '(old) => window.monaco?.editor.getModels().some(model => model.getValue() === old)', arg=previous)
            await page.evaluate('([old, next]) => window.monaco.editor.getModels()'
                '.find(model => model.getValue() === old).setValue(next)', [previous, next_value])
            await expect(tab).to_have_attribute("data-dirty", "true")

        async def save_during_real_lock(button, prior, text):
            fingerprint = digest(text)
            (root / "hold-after-geometry-save").write_text(f"{case_id} {fingerprint}", encoding="utf8")
            async with page.expect_response(lambda response: urlsplit(response.url).path == prefix + "/file"
                and response.request.method == "PUT" and response.request.post_data_json["path"] == GEOMETRY) as saved:
                await page.get_by_role("button", name=button, exact=True).click()
            response = await saved.value
            assert response.request.post_data_json["expected_sha256"] == digest(prior)
            assert response.status == 200, (response.status, await response.text())
            ack = await response.json()
            assert ack["ok"] is True and ack["content_sha256"] == fingerprint and ack["auto_preview_job"] is None
            assert ack["auto_preview_error"]["code"] == "storage_quota_busy", ack
            assert (root / (fingerprint + ".writer-started")).is_file()
            await expect(tabs).to_have_attribute("data-dirty-count", "0")
            await expect(editor_warning).to_contain_text("几何脚本已保存，自动预览未启动")
            await expect(editor_failed).to_have_count(0)
            for _ in range(60):
                if (root / (fingerprint + ".writer-released")).is_file():
                    break
                await asyncio.sleep(.05)
            else:
                raise TimeoutError("Independent SQLite writer did not release")
            committed = await get(prefix + "/file?" + urlencode({"path": GEOMETRY}))
            assert (committed["content"], committed["content_sha256"]) == (text, fingerprint)
            return {"http_status": response.status, "submitted_expected_sha256": digest(prior),
                    "committed_sha256": fingerprint, "preview_error": ack["auto_preview_error"]["code"]}

        async def retained_scene():
            await editor_warning.get_by_role("button", name="打开几何预览").click()
            await expect(geometry_warning).to_contain_text("当前显示上一场景")
            await expect(object_info).to_contain_text("initial_geometry")
            scene = await get(prefix + "/geometry/scene")
            assert scene["publication"]["id"] == settings["seed_publication"] and scene["publication"]["stale"]
            return scene["publication"]["id"]

        await page.goto(base)
        await select_case(CASE)
        initial = source("initial_geometry", 1)
        first = source("single_save_geometry", 2)
        final = source("save_all_geometry", 3)
        # Keep both tabs open so the next save must use the first acknowledgement's
        # digest rather than one fetched by reopening the editor.
        await open_editor(NOTE)
        await edit(GEOMETRY, initial, first)
        evidence["single_save"] = await save_during_real_lock("保存 (Ctrl+S)", initial, first)
        await expect(page.get_by_role("button", name="保存 (Ctrl+S)", exact=True)).to_be_disabled()
        evidence["single_save"]["retained_publication"] = await retained_scene()
        await page.screenshot(path=str(root / "saved-without-preview.png"))
        persist()

        await show_panel("编辑器", tabs)
        await edit(NOTE, ORIGINAL_NOTE, FINAL_NOTE)
        await edit(GEOMETRY, first, final)
        await page.get_by_role("button", name=Path(NOTE).name + " ●", exact=True).click()
        await expect(tabs).to_have_attribute("data-dirty-count", "2")
        evidence["save_all"] = await save_during_real_lock("全部保存", first, final)
        assert (await get(prefix + "/file?" + urlencode({"path": NOTE})))["content"] == FINAL_NOTE
        evidence["save_all"]["retained_publication"] = await retained_scene()
        persist()

        await select_case(OTHER)
        await expect(geometry_warning).to_have_count(0)
        await select_case(CASE)
        rebuild = page.get_by_role("button", name="重建预览", exact=True)
        await show_panel("几何预览", rebuild)
        await expect(geometry_warning).to_contain_text("当前显示上一场景")
        async with page.expect_response(lambda response: urlsplit(response.url).path == prefix + "/geometry:preview"
            and response.request.method == "POST") as submitted:
            await rebuild.click()
        response = await submitted.value
        assert response.status == 200, (response.status, await response.text())
        job_id = (await response.json())["job_id"]
        await expect(object_info).to_contain_text("save_all_geometry", timeout=45000)
        await expect(geometry_warning).to_have_count(0)
        scene = await get(prefix + "/geometry/scene")
        job = await get(f"/api/v1/jobs/{job_id}")
        assert job["status"] == "succeeded" and job["exit_code"] == 0
        assert scene["publication"]["id"] != settings["seed_publication"] and not scene["publication"]["stale"]
        assert [item["name"] for item in scene["objects"]] == ["save_all_geometry"]
        await page.screenshot(path=str(root / "manual-preview-restored.png"))
        evidence["manual_recovery"] = {"job_id": job_id, "exit_code": job["exit_code"],
            "publication": scene["publication"]["id"], "object": scene["objects"][0]["name"]}
        persist()
        await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-autopreview-handoff-browser", app="b05_handoff_fixture:app", access_log=True)
    root = api.root
    options = dict(quota_wait=QUOTA_WAIT, hold_seconds=HOLD_SECONDS, bundle=str(args.dist.resolve(strict=True)))
    (root / "b05_handoff_fixture.py").write_text(APP_SOURCE.format(**options), encoding="utf8")
    temp = root / "browser-tmp"
    temp.mkdir()
    print("GEOMETRY_AUTOPREVIEW_HANDOFF_EVIDENCE", root, flush=True)
    report = {"passed": False, "backend": "fs/sqlite", "model_calls": 0,
              "quota_wait_seconds": QUOTA_WAIT, "real_writer_hold_seconds": HOLD_SECONDS}
    try:
        with api:
            case = api.create_case(CASE)
            workspace = api.call("GET", api.prefix)["workspace_id"]
            api.call("POST", f"/workspaces/{workspace}/cases", {"name": OTHER})
            api.put(NOTE, ORIGINAL_NOTE)
            seeded = api.put(GEOMETRY, source("initial_geometry", 1))
            assert api.wait_job(seeded["auto_preview_job"])["exit_code"] == 0
            initial = api.call("GET", api.prefix + "/geometry/scene")
            settings = {"base": api.base, "case_id": case["id"], "token": api.token,
                        "root": str(root), "channel": args.channel, "seed_publication": initial["publication"]["id"]}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=160,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
        report["passed"] = True
    finally:
        browser_result = root / "browser-handoff.json"
        if browser_result.is_file():
            report["browser"] = json.loads(browser_result.read_text(encoding="utf8"))
        (root / "geometry-handoff-result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print("GEOMETRY_AUTOPREVIEW_HANDOFF_OK", root, flush=True)


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
