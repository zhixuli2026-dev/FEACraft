"""Read real CadQuery bounding boxes and distances with an actual Edge browser."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


CASE = "Bounding box measurement"
OTHER = "Empty measurement case"
SCRIPT_PATH = "A-geo/geometry.py"
LEFT = "left_solid"
RIGHT = "right_solid"


def source(z):
    lines = [
        "import cadquery as cq",
        "def build_geometry(ctx):",
        '    left = cq.Workplane("XY").box(2, 4, 6).translate((1, -2, 0.5))',
    ]
    if z is None:
        lines.append(f"    return {{{LEFT!r}: left}}")
    else:
        lines += [
            f'    right = cq.Workplane("XY").box(4, 2, 2).translate((4, 2, {z!r}))',
            f"    return {{{LEFT!r}: left, {RIGHT!r}: right}}",
        ]
    return "\n".join(lines) + "\n"


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    base, root = settings["base"], Path(settings["root"])
    prefix = f'/api/v1/cases/{settings["case_id"]}'
    headers = {"Authorization": "Bearer " + settings["token"]}
    report = {}
    expect.set_options(timeout=25000)

    def persist():
        (root / "browser-measurement.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8")

    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1490, "height": 1080})
        await context.add_init_script('if(location.origin===' + json.dumps(base) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings["token"]) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)
        tree = page.get_by_test_id("cad-tree")
        properties = page.get_by_test_id("cad-properties")
        compare = page.get_by_role("combobox", name="对比对象")
        measure = page.get_by_test_id("cad-distance")
        left = tree.get_by_role("button", name=LEFT, exact=True)
        right = tree.get_by_role("button", name=RIGHT, exact=True)

        async def select_case(name):
            item = page.locator("[data-case-name=" + json.dumps(name) + "]")
            if await item.get_attribute("aria-pressed") != "true":
                await item.click()
            await expect(item).to_have_attribute("aria-pressed", "true")

        async def geometry_panel():
            toolbar = page.locator("[data-auto-job]")
            if not await toolbar.is_visible():
                await page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^几何预览$")).click()
            await expect(toolbar).to_be_visible()
            if not await tree.is_visible():
                await toolbar.get_by_role("button", name="对象与设置", exact=True).click()
            await expect(tree).to_be_visible()

        async def get(path):
            response = await context.request.get(base + path, headers=headers)
            assert response.status == 200, (response.status, await response.text())
            return await response.json()

        async def read_scene():
            scene = await get(prefix + "/geometry/scene")
            return scene, {item["name"]: item for item in scene["objects"]}

        async def select_object(button, key):
            await button.focus()
            await expect(button).to_be_focused()
            await page.keyboard.press(key)
            await expect(button).to_have_attribute("aria-pressed", "true")

        async def save(previous, next_value):
            row = page.locator(f'[data-path="{SCRIPT_PATH}"]')
            if not await row.is_visible():
                folder = page.locator('[data-path="A-geo"]')
                await expect(folder).to_be_visible()
                if await folder.get_attribute("aria-expanded") == "false":
                    await folder.click()
            await row.click()
            editor = page.locator(f'[data-editor-path="{SCRIPT_PATH}"]')
            await expect(editor).to_have_attribute("data-active", "true")
            await page.wait_for_function('(text) => window.monaco?.editor.getModels().at(-1)?.getValue() === text',
                                         arg=previous)
            await page.evaluate('(text) => window.monaco.editor.getModels().at(-1).setValue(text)', next_value)
            await expect(editor).to_have_attribute("data-dirty", "true")
            async with page.expect_response(lambda response: urlsplit(response.url).path == prefix + "/file"
                and response.request.method == "PUT" and response.request.post_data_json["path"] == SCRIPT_PATH) as submitted:
                await page.get_by_role("button", name="保存 (Ctrl+S)", exact=True).click()
            response = await submitted.value
            assert response.status == 200, (response.status, await response.text())
            return (await response.json())["auto_preview_job"]

        async def job_completed(job_id):
            for _ in range(15):
                job = await get(f"/api/v1/jobs/{job_id}")
                if job["status"] not in ("queued", "running"):
                    assert job["status"] == "succeeded" and job["exit_code"] == 0, job["status"]
                    return
                await asyncio.sleep(.5)
            raise TimeoutError(f"Measurement scene job {job_id} remained in {job['status']}")

        await page.goto(base)
        await select_case(CASE)
        await geometry_panel()
        await expect(left).to_be_visible()
        await expect(right).to_be_visible()
        await expect(properties).to_have_count(0)
        await tree.get_by_role("checkbox", name=f"显示 {LEFT}", exact=True).focus()
        await page.keyboard.press("Tab")
        await expect(left).to_be_focused()
        await select_object(left, "Enter")
        await expect(properties).to_contain_text("包围盒 0, -4, -2.5 ~ 2, 0, 3.5")
        await expect(properties).to_contain_text("尺寸 2 × 4 × 6")
        await expect(properties).to_contain_text("中心 1, -2, 0.5")
        initial, native = await read_scene()
        expected_bounds = {"min": [0.0, -4.0, -2.5], "max": [2.0, 0.0, 3.5]}
        for bound, expected in expected_bounds.items():
            assert all(math.isclose(actual, value, abs_tol=1e-6)
                       for actual, value in zip(native[LEFT]["bbox"][bound], expected)), native[LEFT]["bbox"]
        counts = len(native[LEFT]["vertices"]), len(native[LEFT]["faces"])
        assert counts == (24, 12), counts
        await expect(properties).to_contain_text(f"{counts[0]} 顶点 / {counts[1]} 面")
        await expect(compare.locator("option")).to_have_count(2)
        await compare.select_option(RIGHT)
        await expect(measure).to_have_text("包围盒中心距离 5.000")
        await measure.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / "bbox-distance.png"))
        report["original"] = {"publication": initial["publication"]["id"], "object": LEFT,
            "bbox": native[LEFT]["bbox"], "dimensions": [2, 4, 6], "center": [1, -2, 0.5],
            "vertices": counts[0], "faces": counts[1], "distance": await measure.inner_text(),
            "keyboard_selected": True}
        persist()

        removed_id = await save(source(.5), source(None))
        await geometry_panel()
        await expect(right).to_have_count(0, timeout=45000)
        await expect(left).to_have_attribute("aria-pressed", "true")
        await expect(compare).to_be_disabled()
        await expect(measure).to_have_count(0)
        await job_completed(removed_id)
        removed, remaining = await read_scene()
        assert list(remaining) == [LEFT] and removed["publication"]["id"] != initial["publication"]["id"]
        report["removed_comparison"] = {"publication": removed["publication"]["id"], "job_id": removed_id,
            "original_selection_retained": True, "distance_cleared": True, "choice_disabled": True}
        persist()

        restored_id = await save(source(None), source(12.5))
        await geometry_panel()
        await expect(right).to_be_visible(timeout=45000)
        await expect(compare).to_be_enabled()
        await expect(compare).to_have_value("")
        await expect(measure).to_have_count(0)
        await compare.select_option(RIGHT)
        await expect(measure).to_have_text("包围盒中心距离 13.000")
        await job_completed(restored_id)
        restored, native = await read_scene()
        assert restored["publication"]["id"] != removed["publication"]["id"]
        centers = [[(lo + hi) / 2 for lo, hi in zip(native[key]["bbox"]["min"], native[key]["bbox"]["max"])]
                   for key in (LEFT, RIGHT)]
        assert math.isclose(math.dist(*centers), 13)
        await select_object(right, "Space")
        await expect(compare).to_have_value("")
        await expect(measure).to_have_count(0)
        await expect(properties).to_contain_text("中心 4, 2, 12.5")
        await select_case(OTHER)
        await geometry_panel()
        await expect(properties).to_have_count(0)
        await expect(measure).to_have_count(0)
        report["restored_comparison"] = {"publication": restored["publication"]["id"], "job_id": restored_id,
            "centers": centers, "distance_after_reselect": math.dist(*centers),
            "no_automatic_old_comparison": True, "alternate_case_clear": True}
        persist()
        await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-measurement-browser", app="b10_browser_fixture:app")
    root = api.root
    mounted = args.dist.resolve(strict=True)
    (root / "b10_browser_fixture.py").write_text(
        "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(mounted)) + ", html=True))\n", encoding="utf8")
    temp = root / "browser-tmp"
    temp.mkdir()
    report = {"passed": False, "model_calls": 0, "backend": "fs/sqlite"}
    try:
        with api:
            case = api.create_case(CASE)
            original_case = api.prefix
            workspace = api.call("GET", original_case)["workspace_id"]
            api.call("POST", f"/workspaces/{workspace}/cases", {"name": OTHER})
            saved = api.put(SCRIPT_PATH, source(.5))
            job = api.wait_job(saved["auto_preview_job"])
            assert job["exit_code"] == 0
            settings = {"base": api.base, "case_id": case["id"], "root": str(root), "token": api.token,
                        "channel": args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=175,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            api.create_case("Other measurement account")
            api.call("GET", original_case + "/geometry/scene", expect=404)
            api.call("GET", original_case + "/geometry/validation", expect=404)
            report["different_account_404"] = 2
        report["passed"] = True
    finally:
        observed = root / "browser-measurement.json"
        if observed.is_file():
            report["browser"] = json.loads(observed.read_text(encoding="utf8"))
        (root / "geometry-measurement-result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print("GEOMETRY_MEASUREMENT_BROWSER_OK", root, flush=True)


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
