"""Verify Zebra palettes and Studio texture projection on live CadQuery and Edge."""
import argparse
import asyncio
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys


CASE = "CAD render settings"
OTHER = "Empty render settings case"
PARTS = {"UV plate", "Projection plate"}
GEOMETRY = """import cadquery as cq

def build_geometry(ctx):
    left = cq.Workplane('XY').box(7, 3, 1.4).edges().fillet(0.25).translate((-5, 0, 0))
    right = cq.Workplane('XY').box(6.7, 3.1, 1.2).edges().fillet(0.21).translate((5, 0, 0))
    ctx.show('UV plate', left, color='#eeeeee', material='painted-plastic')
    ctx.show('Projection plate', right, color='#eeeeee')
"""


def write_report(root, name, report):
    (root / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def mesh_instances(message):
    data = message["data"]

    def visit(node):
        if "parts" in node:
            for part in node["parts"]:
                yield from visit(part)
        elif node["type"] == "shapes":
            shape = node["shape"]
            yield node["name"], data["instances"][shape["ref"]] if "ref" in shape else shape

    return dict(visit(data["shapes"]))


def validate_native_uv(scene):
    assert scene["ocp_status"] == {"status": "ready"}
    meshes = mesh_instances(scene["ocp"])
    assert set(meshes) == PARTS, meshes.keys()
    native_uv = meshes["UV plate"]["uvs"]
    position = meshes["UV plate"]["vertices"]
    assert native_uv["codec"] == "b64" and native_uv["dtype"] == "float32"
    assert native_uv["shape"][0] > 0 and native_uv["shape"][0] * 3 == position["shape"][0] * 2
    native_uv_bytes = base64.b64decode(native_uv["buffer"], validate=True)
    assert len(native_uv_bytes) == native_uv["shape"][0] * 4
    no_uv = meshes["Projection plate"].get("uvs")
    assert not no_uv or no_uv.get("shape") == [0], no_uv
    return {"count": native_uv["shape"][0] // 2, "sha256": hashlib.sha256(native_uv_bytes).hexdigest(),
            "uvless_name": "Projection plate"}


async def browser(settings):
    from PIL import Image, ImageChops, ImageDraw
    from playwright.async_api import async_playwright, expect

    root, base = Path(settings["root"]), settings["base"]
    expect.set_options(timeout=25000)
    report = {"passed": False, "checks": {}, "page_errors": [], "shader_errors": [], "model_routes": []}
    checks = report["checks"]
    texture_path = root / "fixture-pattern.png"
    texture = Image.new("RGB", (128, 128), "#cc1506")
    draw = ImageDraw.Draw(texture)
    draw.rectangle((34, 0, 127, 77), fill="#29c228")
    draw.rectangle((0, 78, 83, 127), fill="#0830dd")
    draw.rectangle((84, 78, 127, 127), fill="#f4dc1a")
    draw.rectangle((20, 16, 52, 24), fill="#ffffff")
    texture.save(texture_path)

    def changed(first, second, side="all"):
        assert first.size == second.size
        width, height = first.size
        limits = {"left": (0, 0, width // 2, height), "right": (width // 2, 0, width, height),
                  "all": (0, 0, width, height)}
        values = ImageChops.difference(first.crop(limits[side]), second.crop(limits[side])).split()
        magnitude = ImageChops.lighter(values[0], ImageChops.lighter(values[1], values[2]))
        return sum(magnitude.histogram()[16:])

    try:
        async with async_playwright() as driver:
            edge = await driver.chromium.launch(channel=settings["channel"], args=["--enable-unsafe-swiftshader"])
            context = await edge.new_context(viewport={"width": 1536, "height": 1120}, locale="zh-CN")
            await context.add_init_script(
                "if (location.origin === " + json.dumps(base) + ") localStorage.setItem('vf_token', "
                + json.dumps(settings["token"]) + ");")
            page = await context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            page.on("console", lambda entry: report["shader_errors"].append(entry.text)
                    if entry.type == "error" and re.search(r"WebGLProgram|shader", entry.text, re.I) else None)
            page.on("request", lambda request: report["model_routes"].append(request.url)
                    if request.method == "POST" and re.search(r"/(?:messages|goal:run|turns)(?:[?]|$)", request.url)
                    else None)
            page.set_default_timeout(30000)
            host = page.get_by_test_id("geo-canvas-host")
            canvas = host.locator("canvas")
            tree = page.get_by_test_id("cad-tree")

            async def select_case(name):
                row = page.locator("[data-case-name=" + json.dumps(name) + "]")
                if await row.get_attribute("aria-pressed") != "true":
                    await row.click()
                await expect(row).to_have_attribute("aria-pressed", "true")
                if not await host.is_visible():
                    await page.locator(".dv-tab:visible").filter(has_text=re.compile("^几何预览$")).click()
                if not await tree.is_visible():
                    await page.get_by_role("button", name="对象与设置", exact=True).click()

            async def capture(name):
                await page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                encoded = await canvas.evaluate("canvas => canvas.toDataURL('image/png').split(',')[1]")
                raw = base64.b64decode(encoded, validate=True)
                (root / (name + ".png")).write_bytes(raw)
                return Image.open(BytesIO(raw)).convert("RGB")

            await page.goto(base)
            await select_case(CASE)
            mode = tree.get_by_role("combobox", name="对象树视图")
            try:
                await expect(tree.get_by_role("button", name="UV plate", exact=True)).to_be_visible()
                await expect(mode).to_have_value("flat")
            except AssertionError:
                await page.screenshot(path=root / "initial-readiness-failure.png", timeout=15000)
                raise
            await mode.select_option("assembly")
            await expect(tree.get_by_role("button", name="Projection plate", exact=True)).to_be_visible()
            await page.locator(".geo-view-options > summary").filter(has_text="视角与截图").click()
            await page.get_by_test_id("cad-camera-iso").click()
            zebra = tree.get_by_test_id("cad-zebra")
            scheme = tree.get_by_test_id("cad-zebra-scheme")
            opacity = tree.get_by_test_id("cad-zebra-opacity")
            count = tree.get_by_test_id("cad-zebra-count")
            baseline = await capture("plain-native-scene")
            await zebra.check()
            bw = await capture("zebra-blackwhite")
            await scheme.select_option("colorful")
            colorful = await capture("zebra-colorful")
            await scheme.select_option("grayscale")
            gray = await capture("zebra-grayscale")
            await scheme.select_option("custom")
            await tree.get_by_test_id("cad-zebra-color-first").fill("#fa2407")
            await tree.get_by_test_id("cad-zebra-color-second").fill("#0be384")
            await count.fill("11")
            await count.press("Tab")
            custom = await capture("zebra-custom-odd")
            await tree.get_by_test_id("cad-zebra-color-second").fill("#2c17f2")
            edited = await capture("zebra-custom-edited")
            palette_changes = [changed(baseline, bw), changed(bw, colorful), changed(colorful, gray),
                               changed(gray, custom), changed(custom, edited)]
            assert all(value > 100 for value in palette_changes), palette_changes
            await count.fill("51")
            await count.press("Tab")
            await expect(count).to_have_value("11")
            assert changed(edited, await capture("zebra-invalid-count-retained")) == 0
            await tree.get_by_test_id("cad-zebra-mapping").select_option("reflection")
            reflected = await capture("zebra-reflection")
            assert changed(edited, reflected) > 100
            await tree.get_by_test_id("cad-zebra-angle").press("ArrowRight")
            assert changed(reflected, await capture("zebra-new-direction")) > 20
            await opacity.press("Home")
            zero = await capture("zebra-zero-opacity")
            assert changed(zero, reflected) > 100
            await zebra.uncheck()
            assert changed(baseline, await capture("zebra-off-restored")) == 0
            checks["zebra"] = {"palette_pixel_differences": palette_changes, "bad_count_retained": True,
                               "reflection_and_direction_changed": True, "off_restores": True}

            await page.get_by_test_id("cad-camera-z").click()
            studio = tree.get_by_test_id("cad-studio")
            mapping = tree.get_by_test_id("cad-studio-mapping")
            upload = tree.get_by_test_id("cad-studio-image")
            await studio.check()
            await expect(mapping).to_have_value("triplanar")
            clean_studio = await capture("studio-no-texture")
            await upload.set_input_files(texture_path)
            await expect(tree.get_by_text(texture_path.name, exact=True)).to_be_visible()
            tri = await capture("studio-triplanar")
            tri_deltas = [changed(clean_studio, tri, side) for side in ("left", "right")]
            assert min(tri_deltas) > 2000, tri_deltas
            await mapping.select_option("parametric")
            await expect(tree.get_by_text("没有 UV 的对象使用三平面投影。", exact=True)).to_be_visible()
            uv = await capture("studio-parametric")
            mode_deltas = [changed(tri, uv, side) for side in ("left", "right")]
            assert mode_deltas[0] > 1000 and mode_deltas[1] < 50, mode_deltas
            await page.screenshot(path=root / "studio-controls.png")
            await upload.set_input_files({"name": "broken.png", "mimeType": "image/png", "buffer": b"corrupt"})
            await expect(tree.get_by_role("status").filter(has_text="无法读取这张图片")).to_be_visible()
            assert changed(uv, await capture("studio-invalid-image-retained")) == 0
            await studio.uncheck()
            await studio.check()
            assert changed(uv, await capture("studio-toggle-restored")) == 0
            await tree.get_by_test_id("cad-clip-axis").select_option("x")
            await capture("studio-clipped")
            await tree.get_by_test_id("cad-clip-axis").select_option("")
            assert changed(uv, await capture("studio-after-clip-restored")) == 0
            await tree.get_by_role("button", name="移除纹理").click()
            assert changed(clean_studio, await capture("studio-cleared")) == 0
            await upload.set_input_files(texture_path)
            await expect(tree.get_by_text(texture_path.name, exact=True)).to_be_visible()
            await select_case(OTHER)
            await expect(upload).to_be_disabled()
            await expect(tree.get_by_text(texture_path.name, exact=True)).to_have_count(0)
            await select_case(CASE)
            await expect(tree.get_by_role("button", name="UV plate", exact=True)).to_be_visible()
            await expect(tree.get_by_text(texture_path.name, exact=True)).to_have_count(0)
            checks["studio"] = {"texture_pixels_by_side": tri_deltas, "mode_delta_by_side": mode_deltas,
                                "invalid_preserves": True, "restored_after_clip": True,
                                "removal_and_case_switch": True}
            assert not report["page_errors"] and not report["shader_errors"] and not report["model_routes"], report
            await edge.close()
        report["passed"] = True
    finally:
        write_report(root, "geometry-render-browser.json", report)


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-render-browser", app="render_browser_fixture:app", access_log=True)
    root = api.root
    print("GEOMETRY_RENDER_BROWSER_EVIDENCE", root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    (root / "render_browser_fixture.py").write_text(
        "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n", encoding="utf8")
    browser_tmp = root / "browser-tmp"
    browser_tmp.mkdir()
    report = {"passed": False, "real_model_requests": 0, "mock_llm_calls": 0}
    try:
        primitive = subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "tools/cad-zebra-smoke.mjs")],
                                   capture_output=True, text=True, encoding="utf8", timeout=30)
        (root / "render-primitives.log").write_text(primitive.stdout + primitive.stderr, encoding="utf8")
        primitive.check_returncode()
        with api:
            case = api.create_case(CASE)
            workspace = api.call("GET", api.prefix)["workspace_id"]
            api.call("POST", f"/workspaces/{workspace}/cases", {"name": OTHER})
            route = f"/cases/{case['id']}"
            saved = api.call("PUT", route + "/file", {"path": "A-geo/geometry.py", "content": GEOMETRY})
            job = api.wait_job(saved["auto_preview_job"])
            assert job["exit_code"] == 0 and job["attempt"]["process_stop"]["verified"], job
            scene = api.call("GET", route + "/geometry/scene")
            report["native"] = validate_native_uv(scene) | {
                "job": job["id"], "publication": scene["publication"]["id"], "scene_sha256": digest(scene)}
            browser_settings = {"root": str(root), "base": api.base, "token": api.token, "channel": args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8",
                            str(Path(__file__)), "--browser"], input=json.dumps(browser_settings), text=True,
                           check=True, timeout=280, env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
            assert digest(api.call("GET", route + "/geometry/scene")) == report["native"]["scene_sha256"]
            api.create_case("Other CAD render owner")
            api.call("GET", route + "/geometry/scene", expect=404)
            report["foreign_status"] = 404
        assert api.proc.poll() is not None
        report["passed"] = True
    finally:
        write_report(root, "geometry-render-result.json", report)


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
