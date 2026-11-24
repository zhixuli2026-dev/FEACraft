"""Exercise per-case CAD tree state with actual CadQuery geometry and Edge."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from geometry_base64_browser_acceptance import ASSEMBLY, FIRST, GROUP, REFERENCE, SECOND, SOURCE, native_source


CASE = "B08 nested CAD tree"
OTHER = "B08 separate case"
GEOMETRY = "A-geo/geometry.py"
OTHER_SOURCE = f'''import cadquery as cq

def build_geometry(ctx):
    return {{{FIRST!r}: cq.Workplane("XY").box(2, 4, 6)}}
'''


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    base, root = settings["base"], Path(settings["root"])
    case_route = f'/api/v1/cases/{settings["case_id"]}/geometry/scene'
    expect.set_options(timeout=20000)
    observations = {}

    async with async_playwright() as playwright:
        edge = await playwright.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1550, "height": 1000})
        await context.add_init_script("if(location.origin===" + json.dumps(base)
            + ")localStorage.setItem('vf_token'," + json.dumps(settings["token"]) + ");")
        page = await context.new_page()
        page.set_default_timeout(20000)
        canvas = page.get_by_test_id("geo-canvas-host").locator("canvas")
        controls = page.locator("[data-auto-job]")
        tree = page.get_by_test_id("cad-tree")
        mode = tree.get_by_role("combobox", name="对象树视图")
        group = tree.get_by_role("button", name=GROUP, exact=True)
        group_visible = tree.get_by_role("checkbox", name=f"显示装配 {GROUP}", exact=True)

        def leaf(name):
            return tree.get_by_role("button", name=name, exact=True)

        def visible(name):
            return tree.get_by_role("checkbox", name=f"显示 {name}", exact=True)

        async def open_case(name):
            case = page.locator("[data-case-name=" + json.dumps(name) + "]")
            if await case.get_attribute("aria-pressed") != "true":
                await case.click()
            await expect(case).to_have_attribute("aria-pressed", "true")
            if not await controls.is_visible():
                await page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^几何预览$")).click()
            await expect(canvas).to_be_visible()

        async def canvas_digest(file=None):
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            shot = await canvas.screenshot(path=str(root / file) if file else None, animations="disabled")
            return hashlib.sha256(shot).hexdigest()

        async def keyboard_activate(button):
            await button.focus()
            await expect(button).to_be_focused()
            await page.keyboard.press("Enter")

        try:
            await page.goto(base)
            await open_case(CASE)
            await expect(leaf(ASSEMBLY)).to_be_visible()
            await expect(mode).to_have_value("flat")
            original = await context.request.get(base + case_route,
                headers={"Authorization": "Bearer " + settings["token"]})
            assert original.ok
            publication = (await original.json())["publication"]["id"]
            await mode.select_option("assembly")
            await expect(group).to_have_attribute("aria-expanded", "true")
            await expect(leaf(FIRST)).to_be_visible()
            await expect(leaf(SECOND)).to_be_visible()
            await expect(leaf(REFERENCE)).to_be_visible()
            all_visible = await canvas_digest()

            await visible(SECOND).uncheck()
            assert await group_visible.evaluate("input => input.indeterminate")
            mixed = await canvas_digest()
            assert mixed != all_visible
            await group_visible.click()
            await expect(visible(FIRST)).to_be_checked()
            await expect(visible(SECOND)).to_be_checked()
            await group_visible.click()
            await expect(visible(FIRST)).not_to_be_checked()
            await expect(visible(SECOND)).not_to_be_checked()
            await expect(visible(REFERENCE)).to_be_checked()
            hidden = await canvas_digest()
            assert hidden != mixed and hidden != all_visible
            await visible(FIRST).check()
            unselected = await canvas_digest()
            await keyboard_activate(leaf(FIRST))
            await expect(leaf(FIRST)).to_have_attribute("aria-pressed", "true")
            selected = await canvas_digest("selected-assembly-canvas.png")
            assert selected != unselected
            await page.screenshot(path=str(root / "assembly-tree.png"))
            observations["selection_and_visibility"] = {"partial": mixed, "group_hidden": hidden,
                "selected": selected, "unselected": unselected, "reference_visible": True}

            await keyboard_activate(group)
            await expect(group).to_have_attribute("aria-expanded", "false")
            await expect(leaf(FIRST)).to_have_count(0)
            async with page.expect_response(lambda response: response.url.endswith(case_route)) as reload_scene:
                await controls.get_by_role("button", name="重载场景", exact=True).click()
            assert (await reload_scene.value).ok
            await expect(group).to_have_attribute("aria-expanded", "false")
            await keyboard_activate(group)
            await expect(leaf(FIRST)).to_have_attribute("aria-pressed", "true")
            await expect(visible(SECOND)).not_to_be_checked()
            rebuilt = await canvas_digest()
            await keyboard_activate(leaf(FIRST))
            assert rebuilt == await canvas_digest(), "Selection highlight was not restored to the rebuilt mesh"
            await keyboard_activate(group)
            await controls.get_by_role("button", name="收起对象与设置", exact=True).click()
            await expect(tree).to_have_count(0)

            await page.reload(wait_until="domcontentloaded")
            await open_case(CASE)
            await expect(tree).to_have_count(0)
            await keyboard_activate(controls.get_by_role("button", name="对象与设置", exact=True))
            await expect(mode).to_have_value("assembly")
            await expect(group).to_have_attribute("aria-expanded", "false")
            await keyboard_activate(group)
            await expect(leaf(FIRST)).to_have_attribute("aria-pressed", "true")
            await expect(visible(SECOND)).not_to_be_checked()
            observations["reload"] = {"drawer_collapsed": True, "branch_collapsed": True,
                "leaf_selected": True, "leaf_hidden": True, "mesh_highlight_restored": True}

            await open_case(OTHER)
            await expect(leaf(FIRST)).to_have_attribute("aria-pressed", "false")
            await expect(visible(FIRST)).to_be_checked()
            await expect(mode).to_have_count(0)
            await open_case(CASE)
            await expect(mode).to_have_value("assembly")
            await expect(leaf(FIRST)).to_have_attribute("aria-pressed", "true")
            await mode.select_option("flat")
            await expect(leaf(ASSEMBLY)).to_have_attribute("aria-pressed", "false")
            await expect(visible(ASSEMBLY)).to_be_checked()
            await mode.select_option("assembly")
            await expect(leaf(FIRST)).to_have_attribute("aria-pressed", "true")
            await expect(visible(SECOND)).not_to_be_checked()
            response = await context.request.get(base + case_route,
                headers={"Authorization": "Bearer " + settings["token"]})
            assert response.ok and (await response.json())["publication"]["id"] == publication
            observations["case_and_mode"] = {"distinct_case_defaults": True, "returned_case_restored": True,
                "flat_state_separate": True, "publication_unchanged": publication}
        finally:
            (root / "browser-tree.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf8")
            if "case_and_mode" not in observations:
                await page.screenshot(path=str(root / "tree-failure.png"))
            await edge.close()


def main(args):
    from testsupport import IsolatedServer

    server = IsolatedServer("geometry-tree-browser", app="cad_tree_fixture:app")
    root = server.root
    dist = args.dist.resolve(strict=True)
    (root / "cad_tree_fixture.py").write_text("from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n", encoding="utf8")
    temp = root / "browser-temp"
    temp.mkdir()
    report = {"passed": False, "model_calls": 0, "mock_llm_calls": 0}
    print("GEOMETRY_TREE_EVIDENCE", root, flush=True)
    try:
        with server as api:
            case = api.create_case(CASE)
            original = api.prefix
            workspace = api.call("GET", original)["workspace_id"]
            other = api.call("POST", f"/workspaces/{workspace}/cases", {"name": OTHER})
            for case_id, program in ((case["id"], SOURCE), (other["id"], OTHER_SOURCE)):
                endpoint = f"/cases/{case_id}/file"
                current = api.call("GET", endpoint + "?path=" + GEOMETRY)
                result = api.call("PUT", endpoint, {"path": GEOMETRY, "content": program,
                                                    "expected_sha256": current["content_sha256"]})
                job = api.wait_job(result["auto_preview_job"])
                assert job["exit_code"] == 0
            report["native_assembly"] = native_source(api.call("GET", original + "/geometry/scene"))
            settings = {"base": api.base, "root": str(root), "case_id": case["id"], "token": api.token, "channel": args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=140, env=dict(os.environ, TMP=str(temp), TEMP=str(temp)))
            api.create_case("Unrelated B08 account")
            api.call("GET", original + "/geometry/scene", expect=404)
            report["outside_account_denied"] = True
        report["passed"] = True
    finally:
        browser_result = root / "browser-tree.json"
        if browser_result.exists():
            report["browser"] = json.loads(browser_result.read_text(encoding="utf8"))
        (root / "geometry-tree-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print("GEOMETRY_TREE_BROWSER_OK", root, flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from regression_llm_policy import enforce
        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dist", required=True, type=Path)
        parser.add_argument("--browser-python", required=True, type=Path)
        parser.add_argument("--channel", default="msedge")
        main(parser.parse_args())
