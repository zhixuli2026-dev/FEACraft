"""Verify advanced Studio lighting and independent cameras with actual CadQuery and Edge."""
import argparse
import asyncio
import base64
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys


CASE = "CAD lighting and viewports"
GEOMETRY = """import cadquery as cq

def build_geometry(ctx):
    plinth = cq.Workplane('XY').box(15, 1.4, 10).edges().fillet(0.2).translate((0, -2, 0))
    housing = cq.Workplane('XY').box(6, 6, 4).edges().fillet(0.5).translate((-2.8, 1.4, 0))
    window = cq.Workplane('XY').circle(2).circle(1.2).extrude(1.2).translate((3, -0.25, 2.5))
    ctx.show('Plinth', plinth, color='#b3b9c0')
    ctx.show('Housing', housing, color='#d68b38', material='painted-plastic')
    ctx.show('Window', window, color='#a5c9e6')
"""


STATE = """() => {
  const host = document.querySelector('[data-testid=geo-canvas-host]');
  let fiber = host[Object.keys(host).find(key => key.startsWith('__reactFiber$'))];
  let scene, renderer, camera, quad = [], ao;
  while (fiber) {
    if (typeof fiber.type === 'function') {
      for (let hook = fiber.memoizedState; hook; hook = hook.next) {
        const value = hook.memoizedState?.current;
        if (value?.isScene) scene = value;
        if (value?.isWebGLRenderer) renderer = value;
        if (value?.isCamera && value?.isPerspectiveCamera && !camera) camera = value;
        if (Array.isArray(value) && value[0]?.camera?.isCamera) quad = value;
        if (value?.copyMaterial?.uniforms?.cadAOIntensity) ao = value.copyMaterial.uniforms.cadAOIntensity.value;
      }
    }
    fiber = fiber.return;
  }
  if (!scene || !renderer || !camera) throw new Error('CAD WebGL components unavailable');
  const cameraState = value => ({position: value.position.toArray(), rotation: value.quaternion.toArray(),
    orthographic: value.isOrthographicCamera === true, zoom: value.zoom});
  const items = Object.fromEntries(scene.children.filter(x => x.isMesh && x.userData.vfMesh && !x.userData.clipCap)
    .map(mesh => {
      const material = mesh.userData.originalMaterial;
      return [mesh.name, {clearcoat: material.clearcoat, clearcoatRoughness: material.clearcoatRoughness,
        transmission: material.transmission, ior: material.ior, sheen: material.sheen, map: !!material.map}];
    }));
  const shadow = scene.children.find(x => x.isDirectionalLight).shadow;
  return {camera: cameraState(camera), quad: quad.map(v => ({...cameraState(v.camera), mode: v.mode, preset: v.preset})),
    env: scene.environment?.image?.width ?? null, rotation: scene.environmentRotation.y,
    tone: renderer.toneMapping, exposure: renderer.toneMappingExposure, intensity: scene.environmentIntensity,
    shadow: {enabled: renderer.shadowMap.enabled, intensity: shadow.intensity, radius: shadow.radius},
    gridCount: scene.children.filter(x => x.type === 'GridHelper' && x.visible).length,
    background: scene.background === null ? 'transparent' : scene.background?.type ?? 'color', items, ao};
}"""


def report_to(root, name, report):
    (root / name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")


def scene_hash(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def changed_cameras(before, after):
    def equivalent(a, b):
        for key in ("orthographic", "mode", "preset"):
            if a[key] != b[key]:
                return False
        numbers_a = [*a["position"], *a["rotation"], a["zoom"]]
        numbers_b = [*b["position"], *b["rotation"], b["zoom"]]
        return all(math.isclose(x, y, abs_tol=1e-7) for x, y in zip(numbers_a, numbers_b, strict=True))
    return [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if not equivalent(a, b)]


async def browser(settings):
    from PIL import Image, ImageChops, ImageDraw
    from playwright.async_api import async_playwright, expect

    root, base = Path(settings["root"]), settings["base"]
    expect.set_options(timeout=25000)
    report = {"passed": False, "checks": {}, "hdr": [], "page_errors": [], "shader_errors": [], "model_requests": []}
    checks = report["checks"]
    filename = root / "material-sample.png"
    sample = Image.new("RGB", (96, 96), "#cc281f")
    ImageDraw.Draw(sample).rectangle((0, 10, 38, 78), fill="#1956c7")
    sample.save(filename)

    def pixel_changes(before, after, minimum_delta=12):
        assert before.size == after.size
        bands = ImageChops.difference(before, after).split()
        delta = ImageChops.lighter(bands[0], ImageChops.lighter(bands[1], bands[2]))
        return sum(delta.histogram()[minimum_delta:])

    try:
        async with async_playwright() as driver:
            edge = await driver.chromium.launch(channel=settings["channel"], args=["--enable-unsafe-swiftshader"])
            context = await edge.new_context(viewport={"width": 1536, "height": 1120}, locale="zh-CN")
            await context.add_init_script("if (location.origin === " + json.dumps(base)
                                          + ") localStorage.setItem('vf_token', " + json.dumps(settings["token"]) + ");")
            page = await context.new_page()
            page.set_default_timeout(30000)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            page.on("console", lambda message: report["shader_errors"].append(message.text)
                    if message.type == "error" and re.search(r"WebGLProgram|shader", message.text, re.I) else None)
            page.on("request", lambda request: report["model_requests"].append(request.url)
                    if request.method == "POST" and re.search(r"/(?:messages|goal:run|turns)(?:[?]|$)", request.url)
                    else None)
            host = page.get_by_test_id("geo-canvas-host")
            canvas = host.locator("canvas")
            tree = page.get_by_test_id("cad-tree")

            async def capture(name):
                await page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
                value = await canvas.evaluate("canvas => canvas.toDataURL('image/png').split(',')[1]")
                png = base64.b64decode(value, validate=True)
                (root / (name + ".png")).write_bytes(png)
                return Image.open(BytesIO(png)).convert("RGB")

            async def slider(testid, key):
                control = tree.get_by_test_id(testid)
                await control.press(key)
                return float(await control.input_value())

            async def state():
                return await page.evaluate(STATE)

            async def observe_hdr(resolution, action):
                url_end = f"/studio_small_08_{resolution}.hdr"
                async with page.expect_response(lambda item: item.url.endswith(url_end), timeout=85000) as pending:
                    await action()
                response = await pending.value
                assert response.status == 200, response.status
                width = 1024 if resolution == "1k" else 4096
                await expect(tree.get_by_test_id("cad-studio-resolution")).to_have_text(f"{width} × {width // 2}", timeout=85000)
                actual = await state()
                assert actual["env"] == width, actual["env"]
                report["hdr"].append({"url": response.url, "status": response.status, "width": width,
                                      "content_length": (await response.all_headers()).get("content-length")})

            await page.goto(base)
            case = page.locator("[data-case-name=" + json.dumps(CASE) + "]")
            if await case.get_attribute("aria-pressed") != "true":
                await case.click()
            await expect(case).to_have_attribute("aria-pressed", "true")
            if not await host.is_visible():
                await page.locator(".dv-tab:visible").filter(has_text=re.compile("^几何预览$")).click()
            if not await tree.is_visible():
                await page.get_by_role("button", name="对象与设置", exact=True).click()
            await expect(tree.get_by_role("button", name="Housing", exact=True)).to_be_visible()
            await page.locator(".geo-view-options > summary").filter(has_text="视角与截图").click()
            await page.get_by_test_id("cad-fit").click()
            plain = await capture("single-baseline")
            assert set((await state())["items"]) == {"Plinth", "Housing", "Window"}
            assert (await state())["gridCount"] == 1
            await tree.get_by_test_id("cad-grid-xy").check()
            await tree.get_by_test_id("cad-grid-yz").check()
            assert (await state())["gridCount"] == 3
            await tree.get_by_test_id("cad-grid-center").check()
            grid_pixels = pixel_changes(plain, await capture("centered-three-grids"))
            assert grid_pixels > 100, grid_pixels
            await tree.get_by_test_id("cad-grid-center").uncheck()
            await tree.get_by_test_id("cad-grid-xy").uncheck()
            await tree.get_by_test_id("cad-grid-yz").uncheck()
            checks["coordinates"] = {"grid_pixels": grid_pixels, "three_planes": True}

            views = tree.get_by_test_id("cad-view-mode")
            await views.select_option("quad")
            first = host.get_by_test_id("cad-viewport-0")
            last = host.get_by_test_id("cad-viewport-3")
            await expect(last).to_be_visible()
            await capture("quad-before")
            baseline = (await state())["quad"]
            assert [record["preset"] for record in baseline] == ["iso", "x", "y", "z"]
            assert len({tuple(record["rotation"]) for record in baseline}) == 4
            bounds = await first.bounding_box()
            x, y = bounds["x"] + bounds["width"] * 0.5, bounds["y"] + bounds["height"] * 0.5
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.move(x + 36, y + 27, steps=8)
            await page.mouse.up()
            await capture("quad-one-orbit")
            after_orbit = (await state())["quad"]
            assert changed_cameras(baseline, after_orbit) == [0]
            await page.mouse.wheel(0, -135)
            await capture("quad-one-zoom")
            after_zoom = (await state())["quad"]
            assert changed_cameras(after_orbit, after_zoom) == [0]
            await last.focus()
            await expect(last).to_have_attribute("data-active", "true")
            await tree.get_by_test_id("cad-control").select_option("trackball")
            await tree.get_by_test_id("cad-ortho").check()
            await page.get_by_test_id("cad-camera-y").click()
            await page.get_by_test_id("cad-fit").click()
            await capture("quad-independent-projection")
            final_views = (await state())["quad"]
            assert changed_cameras(after_zoom, final_views) == [3]
            assert final_views[3]["orthographic"] and final_views[3]["mode"] == "trackball" and final_views[3]["preset"] == "y"
            await views.select_option("single")
            await expect(tree.get_by_test_id("cad-ortho")).not_to_be_checked()
            await expect(tree.get_by_test_id("cad-control")).to_have_value("orbit")
            await views.select_option("quad")
            await capture("quad-reentered")
            assert changed_cameras(final_views, (await state())["quad"]) == []
            await views.select_option("single")
            checks["independent_views"] = {"orbit_and_zoom": [0], "projection_mode_preset_fit": [3],
                                           "retained_after_toggle": True}

            studio = tree.get_by_test_id("cad-studio")
            await studio.check()
            await capture("procedural-studio")
            tone = tree.get_by_test_id("cad-studio-tone")
            background = tree.get_by_test_id("cad-studio-background")
            environment = tree.get_by_test_id("cad-studio-environment")
            highres = tree.get_by_test_id("cad-studio-4k")
            await expect(highres).to_be_disabled()
            await observe_hdr("1k", lambda: environment.select_option("studio_small_08"))
            image_1k = await capture("studio-photo-1k")
            await observe_hdr("4k", highres.check)
            image_4k = await capture("studio-photo-4k")
            # Downsampled screenshots of the same HDRI can differ by only one channel level.
            resolution_delta = pixel_changes(image_1k, image_4k, minimum_delta=1)
            assert resolution_delta > 10, resolution_delta
            rotate = await slider("cad-studio-rotation", "PageUp")
            assert 0 < rotate < 360 and abs((await state())["rotation"] - math.radians(rotate)) < 1e-6
            rotated = await capture("studio-photo-rotated")
            rotation_delta = pixel_changes(image_4k, rotated)
            assert rotation_delta > 100, rotation_delta
            await slider("cad-studio-exposure", "Home")
            low = await capture("studio-exposure-zero")
            await slider("cad-studio-exposure", "End")
            bright = await capture("studio-exposure-three")
            assert pixel_changes(low, bright) > 1000 and (await state())["exposure"] == 3
            await tone.select_option("ACES")
            aces = await capture("studio-aces")
            await tone.select_option("none")
            linear = await capture("studio-linear")
            tone_delta = [pixel_changes(bright, aces), pixel_changes(aces, linear)]
            assert min(tone_delta) > 100, tone_delta
            await tone.select_option("neutral")
            await background.select_option("gradient-dark")
            gradient = await capture("studio-gradient")
            await background.select_option("transparent")
            transparent = await capture("studio-transparent")
            assert (await state())["background"] == "transparent" and pixel_changes(gradient, transparent) > 1000
            await background.select_option("grey")
            await context.set_offline(True)
            await environment.select_option("studio_small_03")
            await expect(tree.get_by_role("status").filter(has_text="环境图加载失败，请重试。")).to_be_visible()
            await context.set_offline(False)
            await expect(environment).to_have_value("studio_small_08")
            assert (await state())["env"] == 4096
            await environment.select_option("studio")
            await expect(tree.get_by_test_id("cad-studio-resolution")).to_have_count(0)
            checks["lighting"] = {"1k_vs_4k_pixels": resolution_delta, "rotation_pixels": rotation_delta,
                                   "tone_change_pixels": tone_delta, "actual_exposure": True,
                                   "mode_and_failed_fetch_restored": True}

            await tree.get_by_test_id("cad-shadow").check()
            await slider("cad-studio-shadowIntensity", "Home")
            no_shadow = await capture("studio-shadow-zero")
            await slider("cad-studio-shadowIntensity", "End")
            sharp_shadow = await capture("studio-shadow-full")
            shadow_pixels = pixel_changes(no_shadow, sharp_shadow)
            assert (await state())["shadow"]["intensity"] == 1 and shadow_pixels > 10, shadow_pixels
            await slider("cad-studio-shadowSoftness", "End")
            soft_shadow = await capture("studio-shadow-soft")
            softness_pixels = pixel_changes(sharp_shadow, soft_shadow)
            assert (await state())["shadow"]["radius"] == 8 and softness_pixels > 10, softness_pixels
            await tree.get_by_test_id("cad-shadow").uncheck()
            await tree.get_by_test_id("cad-ao").check()
            await slider("cad-ao-intensity", "Home")
            no_ao = await capture("studio-ao-zero")
            await slider("cad-ao-intensity", "End")
            max_ao = await capture("studio-ao-three")
            ao_pixels = pixel_changes(no_ao, max_ao)
            assert ao_pixels > 10 and (await state())["ao"] == 3, ao_pixels
            await views.select_option("quad")
            await expect(tree.get_by_test_id("cad-ao")).not_to_be_checked()
            await views.select_option("single")
            checks["occlusion"] = {"shadow_pixels": shadow_pixels, "softness_pixels": softness_pixels,
                                   "ao_pixels": ao_pixels, "quad_to_ao_compatibility": True}

            await tree.get_by_role("button", name="Window", exact=True).click()
            await tree.locator("details > summary").filter(has_text="表面材质").click()
            material = tree.get_by_test_id("cad-surface-preset")
            await expect(material).to_have_value("standard")
            original = await capture("studio-standard-material")
            await material.select_option("glass")
            glass = await capture("studio-window-glass")
            items = (await state())["items"]
            glass_pixels = pixel_changes(original, glass)
            assert glass_pixels > 50 and items["Window"]["transmission"] == 0.96
            assert items["Plinth"]["transmission"] == 0 and items["Housing"]["transmission"] == 0
            await material.select_option("coated")
            assert (await state())["items"]["Window"]["clearcoat"] == 0.9
            await material.select_option("satin")
            assert (await state())["items"]["Window"]["sheen"] == 0.9
            await slider("cad-surface-ior", "End")
            assert (await state())["items"]["Window"]["ior"] == 2.33
            await material.select_option("standard")
            upload = tree.get_by_test_id("cad-studio-image")
            await upload.set_input_files(filename)
            await expect(tree.get_by_text(filename.name, exact=True)).to_be_visible()
            assert all(item["map"] for item in (await state())["items"].values())
            await tree.get_by_test_id("cad-studio-mapping").select_option("parametric")
            await page.screenshot(path=root / "studio-material-interfaces.png")
            await studio.uncheck()
            assert all(not item["map"] for item in (await state())["items"].values())
            checks["materials"] = {"single_object_glass_pixels": glass_pixels, "physical_properties": True,
                                    "legacy_texture_mapping": True}
            assert not report["page_errors"] and not report["shader_errors"] and not report["model_requests"], report
            await edge.close()
        report["passed"] = True
    finally:
        report_to(root, "studio-view-browser.json", report)


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-studio-view", app="studio_view_fixture:app", access_log=True)
    root = api.root
    print("GEOMETRY_STUDIO_VIEW_EVIDENCE", root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    (root / "studio_view_fixture.py").write_text(
        "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n", encoding="utf8")
    browser_tmp = root / "browser-tmp"
    browser_tmp.mkdir()
    report = {"passed": False, "real_model_requests": 0, "mock_llm_calls": 0}
    try:
        primitives = subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "tools/cad-view-smoke.mjs")],
                                    capture_output=True, text=True, encoding="utf8", timeout=30)
        (root / "studio-view-primitives.log").write_text(primitives.stdout + primitives.stderr, encoding="utf8")
        primitives.check_returncode()
        with api:
            case = api.create_case(CASE)
            route = f"/cases/{case['id']}"
            saved = api.call("PUT", route + "/file", {"path": "A-geo/geometry.py", "content": GEOMETRY})
            job = api.wait_job(saved["auto_preview_job"])
            assert job["exit_code"] == 0 and job["attempt"]["process_stop"]["verified"], job
            document = api.call("GET", route + "/geometry/scene")
            assert document["ocp_status"] == {"status": "ready"}
            assert set(item["name"] for item in document["objects"]) == {"Plinth", "Housing", "Window"}
            report["native"] = {"job": job["id"], "publication": document["publication"]["id"],
                                "scene_sha256": scene_hash(document)}
            settings = {"root": str(root), "base": api.base, "token": api.token, "channel": args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8",
                            str(Path(__file__)), "--browser"], input=json.dumps(settings), text=True,
                           check=True, timeout=420, env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
            assert scene_hash(api.call("GET", route + "/geometry/scene")) == report["native"]["scene_sha256"]
            api.create_case("Other Studio view owner")
            api.call("GET", route + "/geometry/scene", expect=404)
            report["foreign_status"] = 404
        assert api.proc.poll() is not None
        report["passed"] = True
    finally:
        report_to(root, "studio-view-result.json", report)


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
