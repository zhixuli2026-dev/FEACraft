"""Verify the local Base64 CAD viewer bridge using native CadQuery and actual Edge."""

import argparse
import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys


CASE = "Local CAD base64 scene"
ASSEMBLY = "server_assembly"
REFERENCE = "server_reference"
GROUP = "装配组"
FIRST = "重复实例甲"
SECOND = "重复实例乙"
FLAT = "平铺三角·原样"
SCRIPT_PATH = "A-geo/geometry.py"

SOURCE = '''
import cadquery as cq

def build_geometry(ctx):
    reusable = cq.Workplane("XY").box(2, 4, 6)
    axis = cq.Vector(0, 0, 1)
    group = cq.Assembly(name="装配组")
    group.add(reusable, name="重复实例甲", loc=cq.Location(cq.Vector(3, 0, 0)))
    group.add(reusable, name="重复实例乙", loc=cq.Location(cq.Vector(-2, 1, 4), axis, 90))
    hardware = cq.Assembly(name="native_fixture_assembly")
    hardware.add(group, loc=cq.Location(cq.Vector(10, 20, 2), axis, 90))
    reference = cq.Workplane("XY").box(5, 6, 7).translate((-4, -3, 1))
    return {"server_assembly": hardware, "server_reference": reference}
'''


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")


def encode(document):
    return base64.b64encode(json_bytes(document)).decode("ascii")


def native_nodes(node):
    yield node
    for part in node.get("parts", []):
        yield from native_nodes(part)


def native_ref_leaves(payload):
    return {node["name"]: node for node in native_nodes(payload["data"]["shapes"])
            if isinstance(node.get("shape"), dict) and "ref" in node["shape"]}


def native_source(scene):
    payload = scene["ocp"]
    assert scene["ocp_status"] == {"status": "ready"} and payload["type"] == "data"
    assert {item["name"] for item in scene["objects"]} == {ASSEMBLY, REFERENCE}
    groups = {node["name"]: node for node in native_nodes(payload["data"]["shapes"]) if node.get("parts")}
    group = groups[GROUP]
    assert len(group["parts"]) == 2 and group["loc"] is not None
    translation, rotation = group["loc"]
    assert all(math.isclose(value, expected, abs_tol=1e-8)
               for value, expected in zip(translation, [10, 20, 2], strict=True)), translation
    assert math.isclose(abs(rotation[2]), math.sqrt(.5), abs_tol=1e-8), rotation
    leaves = native_ref_leaves(payload)
    shared = leaves[FIRST]["shape"]["ref"]
    assert leaves[SECOND]["shape"]["ref"] == shared
    placements = {name: groups[name]["loc"] for name in (FIRST, SECOND)}
    assert placements[FIRST][0] == [3, 0, 0] and placements[SECOND][0] == [-2, 1, 4]
    mesh = payload["data"]["instances"][shared]
    assert math.prod(mesh["vertices"]["shape"]) >= 24 and math.prod(mesh["triangles"]["shape"]) >= 12
    return {"shared_ref": shared, "instance_buffers": len(payload["data"]["instances"]),
            "group_loc": group["loc"], "leaf_names": [FIRST, SECOND], "placements": placements}


def flat_source(scene):
    original = next(item for item in scene["objects"] if item["name"] == REFERENCE)
    low, high = original["bbox"]["min"], original["bbox"]["max"]
    return {"name": FLAT, "color": original["color"], "alpha": original["alpha"],
            "vertices": [value for vertex in original["vertices"] for value in vertex],
            "triangles": [index for face in original["faces"] for index in face],
            "bb": {axis + suffix: bounds[i] for suffix, bounds in (("min", low), ("max", high))
                   for i, axis in enumerate("xyz")}}


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    base = settings["base"]
    root = Path(settings["root"])
    canonical = settings["baseline"]
    payload = canonical["ocp"]
    flat = flat_source(canonical)
    bad = deepcopy(payload)
    native_ref_leaves(bad)[SECOND]["shape"]["ref"] = len(payload["data"]["instances"]) + 7
    prefix = f'/api/v1/cases/{settings["case_id"]}'
    headers = {"Authorization": "Bearer " + settings["token"]}
    report = {}
    expect.set_options(timeout=25000)

    def persist():
        (root / "browser-base64.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")

    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1490, "height": 1080})
        await context.add_init_script('if(location.origin===' + json.dumps(base)
            + ')localStorage.setItem("vf_token",' + json.dumps(settings["token"]) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)
        host = page.get_by_test_id("geo-canvas-host")
        canvas = host.locator("canvas")
        tree = page.get_by_test_id("cad-tree")
        status = page.get_by_test_id("geo-status")
        props = page.get_by_test_id("cad-properties")
        rows = tree.locator("[data-object-name]")

        def row(name):
            return rows.filter(has=page.get_by_role("button", name=re.compile(re.escape(name) + r"$")))

        async def select(name):
            item = row(name)
            await expect(item).to_have_count(1)
            await item.get_by_role("button").click()
            await expect(props).to_be_visible()
            return await props.inner_text()

        async def load(document):
            await page.evaluate("(input) => window.vfLoadSceneFromBase64(input)", encode(document))
            await expect(status).to_contain_text("Base64 场景已加载")

        async def reject(encoded):
            await page.evaluate("(input) => window.vfLoadSceneFromBase64(input)", encoded)
            await expect(status).to_contain_text("Base64 场景加载失败，保留上一有效场景")

        async def screenshot(filename):
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            image = await canvas.screenshot(path=str(root / filename), animations="disabled")
            return hashlib.sha256(image).hexdigest()

        await page.goto(base)
        case = page.locator("[data-case-name=" + json.dumps(CASE) + "]")
        if await case.get_attribute("aria-pressed") != "true":
            await case.click()
        await expect(case).to_have_attribute("aria-pressed", "true")
        toolbar = page.locator("[data-auto-job]")
        if not await toolbar.is_visible():
            await page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^几何预览$")).click()
        await expect(toolbar).to_be_visible()
        if not await tree.is_visible():
            await toolbar.get_by_role("button", name="对象与设置", exact=True).click()
        await expect(canvas).to_be_visible()
        await expect(row(REFERENCE)).to_be_visible()
        functions = await page.evaluate("""() => [
            "vfLoadSceneFromBase64", "vfFit", "vfSetEdges", "vfSetTransparent", "setVibeFlowTheme",
        ].map(name => ({name, type: typeof window[name]}))""")
        assert all(item["type"] == "function" for item in functions), functions

        await page.evaluate("() => { window.vfSetEdges(true); window.vfSetTransparent(false); window.setVibeFlowTheme('dark'); window.vfFit(); }")
        await expect(host).to_have_attribute("data-theme", "dark")
        canvas_baseline = await screenshot("cad-native-before.png")

        await page.evaluate("() => window.vfSetEdges(false)")
        await expect(host).to_have_attribute("data-edges", "false")
        without_edges = await screenshot("cad-no-edges.png")
        assert without_edges != canvas_baseline
        await page.evaluate("() => window.vfSetTransparent(true)")
        await expect(host).to_have_attribute("data-transparent", "true")
        transparent = await screenshot("cad-transparent.png")
        assert transparent != without_edges
        await page.evaluate("() => window.setVibeFlowTheme('light')")
        await expect(host).to_have_attribute("data-theme", "light")
        await expect(page.locator("html")).to_have_attribute("data-vf-theme", "light")
        await expect(page.locator("body")).to_have_attribute("data-vf-theme", "light")
        light = await screenshot("cad-light.png")
        assert light != transparent
        await canvas.hover(position={"x": 70, "y": 70})
        await page.mouse.wheel(0, -500)
        zoomed = await screenshot("cad-zoomed.png")
        await page.evaluate("() => window.vfFit()")
        fitted = await screenshot("cad-fitted.png")
        assert zoomed != light and fitted != zoomed
        report["display_bridge"] = {"functions": [item["name"] for item in functions],
            "render_hashes": {"baseline": canvas_baseline, "no_edges": without_edges,
                "transparent": transparent, "light": light, "zoomed": zoomed, "fitted": fitted}}
        persist()

        await load({"ocp": payload, "objects": [flat]})
        await expect(row(FLAT)).to_have_count(0)
        await expect(row(FIRST)).to_be_visible()
        await expect(row(SECOND)).to_be_visible()
        await expect(host).to_have_attribute("data-edges", "false")
        await expect(host).to_have_attribute("data-transparent", "true")
        await expect(host).to_have_attribute("data-theme", "light")
        readings = {}
        for name, center, sizes, bounds in (
            (FIRST, "10, 23, 2", "4 × 2 × 6", "8, 22, -1 ~ 12, 24, 5"),
            (SECOND, "9, 18, 6", "2 × 4 × 6", "8, 16, 3 ~ 10, 20, 9"),
        ):
            shown = await select(name)
            await expect(props).to_contain_text("包围盒 " + bounds)
            await expect(props).to_contain_text("中心 " + center)
            await expect(props).to_contain_text("尺寸 " + sizes)
            counts = re.search(r"(\d+) 顶点 / (\d+) 面", shown)
            assert counts and int(counts[1]) >= 8 and int(counts[2]) >= 12, shown
            readings[name] = {"center": center, "size": sizes, "bbox": bounds,
                              "vertices": int(counts[1]), "faces": int(counts[2])}
        ocp_picture = await screenshot("cad-native-ocp.png")
        assert ocp_picture != fitted

        await load({"ocpMessage": payload, "objects": [flat]})
        await expect(row(FIRST)).to_be_visible()
        await expect(row(SECOND)).to_be_visible()
        await expect(row(FLAT)).to_have_count(0)
        await load(payload)
        await expect(row(FIRST)).to_be_visible()
        await expect(row(SECOND)).to_be_visible()
        report["native_ocp"] = {"wrappers": ["ocp", "ocpMessage", "raw data"], "readings": readings,
                                "render_sha256": ocp_picture}
        persist()

        await reject("%%%")
        await expect(row(FIRST)).to_be_visible()
        await expect(row(SECOND)).to_be_visible()
        await reject(encode({"ocp": bad}))
        await expect(status).to_contain_text("OCP 网格实例索引无效")
        await expect(row(FIRST)).to_be_visible()
        await expect(row(SECOND)).to_be_visible()
        await expect(rows).to_have_count(3)
        report["malformed_base64_and_unresolved_ref_preserved_scene"] = True

        await load({"ocp": bad, "objects": [flat]})
        await expect(row(FLAT)).to_be_visible()
        await expect(rows).to_have_count(1)
        shown = await select(FLAT)
        assert "24 顶点 / 12 面" in shown, shown
        await expect(props).to_contain_text("尺寸 5 × 6 × 7")
        await expect(props).to_contain_text("中心 -4, -3, 1")
        flat_picture = await screenshot("cad-flat-fallback.png")
        assert flat_picture != ocp_picture
        await load({"objects": [flat]})
        await expect(row(FLAT)).to_be_visible()
        await expect(rows).to_have_count(1)
        report["ordinary_flat"] = {"fallback_from_invalid_ocp": True, "ordinary_only": True,
            "name": FLAT, "render_sha256": flat_picture}

        await load({"objects": []})
        await expect(rows).to_have_count(0)
        await expect(props).to_have_count(0)
        await toolbar.get_by_role("button", name="重载场景", exact=True).click()
        await expect(row(ASSEMBLY)).to_be_visible()
        await expect(row(REFERENCE)).to_be_visible()
        await expect(rows).to_have_count(2)
        response = await context.request.get(base + prefix + "/geometry/scene", headers=headers)
        assert response.status == 200
        readback = await response.json()
        assert readback == canonical
        report["server_round_trip"] = {"empty_local_scene": True, "reload_restored_publication": canonical["publication"]["id"],
            "unchanged_scene_sha256": hashlib.sha256(json_bytes(readback)).hexdigest()}
        persist()
        await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-base64-browser", app="b07_browser_fixture:app")
    root = api.root
    bundle = args.dist.resolve(strict=True)
    (root / "b07_browser_fixture.py").write_text(
        "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(bundle)) + ", html=True))\n", encoding="utf8")
    temp = root / "browser-tmp"
    temp.mkdir()
    report = {"passed": False, "model_calls": 0, "backend": "fs/sqlite"}
    print("GEOMETRY_BASE64_BROWSER_EVIDENCE", root, flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            case_path = api.prefix
            save = api.put(SCRIPT_PATH, SOURCE)
            job = api.wait_job(save["auto_preview_job"])
            assert job["exit_code"] == 0
            scene = api.call("GET", case_path + "/geometry/scene")
            report["native_fixture"] = native_source(scene)
            arguments = {"base": api.base, "case_id": case["id"], "root": str(root), "token": api.token,
                         "channel": args.channel, "baseline": scene}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(arguments), text=True, check=True, timeout=155,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            api.create_case("Other CAD bridge account")
            api.call("GET", case_path + "/geometry/scene", expect=404)
            report["different_account_404"] = 1
        report["passed"] = True
    finally:
        observed = root / "browser-base64.json"
        if observed.is_file():
            report["browser"] = json.loads(observed.read_text(encoding="utf8"))
        (root / "geometry-base64-result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8")
    print("GEOMETRY_BASE64_BROWSER_OK", root, flush=True)


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
