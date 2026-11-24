"""Actual Edge preview tolerance and CadQuery validation/readback without model requests."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit


CHOSEN = 0.025
CASE = "Geometry preview tolerance"
SCRIPT_PATH = "A-geo/geometry.py"
FALLBACK_OBJECT = "fallback_round"
FALLBACK_SOURCE = 'import sys\nsys.modules["ocp_tessellate"] = None\n'


def script(name):
    return ('import cadquery as cq\n'
            'def build_geometry(ctx):\n'
            '    print(f"B03_NATIVE_TOLERANCE={ctx.tolerance}", flush=True)\n'
            f'    return {{{name!r}: cq.Workplane("XY").circle(3).extrude(2)}}\n')


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    api = settings["base"] + f'/api/v1/cases/{settings["case_id"]}'
    path = urlsplit(api).path
    headers = {"Authorization": "Bearer " + settings["token"]}
    root = Path(settings["root"])
    checks = {}
    expect.set_options(timeout=25000)
    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1450, "height": 940})
        await context.add_init_script('if(location.origin===' + json.dumps(settings["base"]) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings["token"]) + ');')
        page = await context.new_page()
        page.set_default_timeout(20000)
        tolerance = page.get_by_label("预览容差", exact=True)
        rebuild = page.get_by_role("button", name="重建预览", exact=True)
        applied = page.get_by_test_id("preview-tolerance-applied")
        form_requests = []

        def observe(request):
            if request.method == "POST" and urlsplit(request.url).path == path + "/geometry:preview":
                form_requests.append(request.post_data_json)

        page.on("request", observe)

        async def open_geometry(reload=False):
            await (page.reload() if reload else page.goto(settings["base"]))
            selected = page.locator("[data-case-name=" + json.dumps(CASE) + "]")
            if await selected.get_attribute("aria-pressed") != "true":
                await selected.click()
            if not await tolerance.is_visible():
                await page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^几何预览$")).click()
            await expect(tolerance).to_be_visible()

        async def job_ended(job_id, value):
            until = time.monotonic() + 45
            while time.monotonic() < until:
                response = await context.request.get(settings["base"] + f"/api/v1/jobs/{job_id}", headers=headers)
                assert response.status == 200, await response.text()
                job = await response.json()
                if job["status"] not in ("queued", "running"):
                    assert job["status"] == "succeeded" and job["exit_code"] == 0, job["log_backlog"]
                    assert f"B03_NATIVE_TOLERANCE={value}" in job["log_backlog"]
                    return job
                await asyncio.sleep(.1)
            raise TimeoutError(f"Actual CadQuery preview did not finish: {job_id}")

        async def geometry_document(kind):
            response = await context.request.get(api + "/geometry/" + kind, headers=headers)
            assert response.status == 200, await response.text()
            return await response.json()

        async def save_source(previous, updated):
            row = page.locator(f'[data-path="{SCRIPT_PATH}"]')
            if not await row.is_visible():
                folder = page.locator('[data-path="A-geo"]')
                await expect(folder).to_be_visible()
                if await folder.get_attribute("aria-expanded") == "false":
                    await folder.click()
            await row.click()
            await expect(page.locator(f'[data-editor-path="{SCRIPT_PATH}"]')).to_have_attribute("data-active", "true")
            await page.wait_for_function('(text) => window.monaco?.editor.getModels().at(-1)?.getValue() === text',
                                         arg=previous)
            await page.evaluate('(text) => window.monaco.editor.getModels().at(-1).setValue(text)', updated)
            async with page.expect_response(lambda response: urlsplit(response.url).path == path + "/file"
                and response.request.method == "PUT") as saved:
                await page.get_by_role("button", name="保存 (Ctrl+S)", exact=True).click()
            response = await saved.value
            assert response.status == 200, await response.text()
            return (await response.json())["auto_preview_job"]

        await open_geometry()
        await expect(tolerance).to_have_value("0.1")
        if "fallback_initial_publication" in settings:
            await expect(applied).to_contain_text("已应用容差 0.1")
            await expect(applied).to_contain_text("三角网格回退（OCP 不可用）")
            await tolerance.fill(str(CHOSEN))
            async with page.expect_response(lambda response: urlsplit(response.url).path == path + "/geometry:preview"
                and response.request.method == "POST") as admitted:
                await rebuild.click()
            response = await admitted.value
            assert response.status == 200, await response.text()
            job_id = (await response.json())["job_id"]
            await expect(applied).to_contain_text(f"已应用容差 {CHOSEN}", timeout=45000)
            job = await job_ended(job_id, CHOSEN)
            assert "OCP preview fell back to the triangle mesh: ModuleNotFoundError" in job["log_backlog"]
            await expect(applied).to_contain_text("三角网格回退（OCP 不可用）")
            info = page.get_by_test_id("geo-obj-info")
            await expect(info).to_contain_text(FALLBACK_OBJECT)
            await expect(page.locator(".geo-canvas canvas")).to_be_visible()
            scene = await geometry_document("scene")
            validation = await geometry_document("validation")
            status = {"status": "fallback", "reason": "ocp_unavailable"}
            assert scene["ocp_status"] == status and "ocp" not in scene
            assert scene["publication"]["id"] == validation["publication"]["id"]
            assert scene["publication"]["id"] != settings["fallback_initial_publication"]
            assert scene["tolerance"] == validation["tolerance"] == CHOSEN
            assert validation["summary"]["valid"]
            mesh = scene["objects"][0]
            vertices, faces = mesh["vertices"], mesh["faces"]
            assert mesh["name"] == FALLBACK_OBJECT and len(vertices) >= 3 and faces
            assert all(len(face) == 3 and all(0 <= index < len(vertices) for index in face) for face in faces)
            assert form_requests == [{"tolerance": CHOSEN}], form_requests
            checks["fallback"] = {"job_id": job_id, "native_exit_code": job["exit_code"],
                "publication": scene["publication"]["id"], "applied": scene["tolerance"], "ocp": status,
                "vertex_count": len(vertices), "triangle_count": len(faces), "canvas_visible": True,
                "initial_publication": settings["fallback_initial_publication"]}
            await applied.scroll_into_view_if_needed()
            await page.screenshot(path=str(root / "fallback-rounded-preview.png"))
            (root / "browser-result.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf8")
            await edge.close()
            return

        await expect(applied).to_contain_text("已应用容差 0.1")
        for invalid in ("0", "-0.5", ""):
            await tolerance.fill(invalid)
            await expect(tolerance).to_have_attribute("aria-invalid", "true")
            await expect(rebuild).to_be_disabled()
        assert form_requests == []
        checks["invalid_input_never_submitted"] = True

        await tolerance.fill(str(CHOSEN))
        async with page.expect_response(lambda response: urlsplit(response.url).path == path + "/geometry:preview"
            and response.request.method == "POST") as admitted:
            await rebuild.click()
        response = await admitted.value
        assert response.status == 200, await response.text()
        manual = (await response.json())["job_id"]
        assert form_requests == [{"tolerance": CHOSEN}]
        await job_ended(manual, CHOSEN)
        await expect(applied).to_contain_text(f"已应用容差 {CHOSEN}")
        scene = await geometry_document("scene")
        validation = await geometry_document("validation")
        assert scene["tolerance"] == validation["tolerance"] == CHOSEN
        assert scene["publication"]["id"] == validation["publication"]["id"]
        assert scene["objects"][0]["faces"]
        assert scene["ocp_status"]["status"] == "ready" and scene["ocp"], scene["ocp_status"]
        await expect(applied).to_contain_text("OCP 几何数据就绪")
        checks["manual"] = {"job_id": manual, "applied": CHOSEN, "publication": scene["publication"]["id"],
                            "triangle_count": len(scene["objects"][0]["faces"]), "ocp": scene["ocp_status"]}
        await applied.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / "nondefault-preview-tolerance.png"))

        await open_geometry(reload=True)
        await expect(tolerance).to_have_value(str(CHOSEN))
        await expect(applied).to_contain_text(f"已应用容差 {CHOSEN}")
        checks["manual_preference_survived_reload"] = True
        automatic = await save_source(script("round_initial"), script("round_revised"))
        await job_ended(automatic, 0.1)
        await expect(tolerance).to_have_value(str(CHOSEN))
        await expect(applied).to_contain_text("已应用容差 0.1")
        final_scene = await geometry_document("scene")
        assert final_scene["tolerance"] == 0.1 and final_scene["objects"][0]["name"] == "round_revised"
        assert final_scene["publication"]["id"] != scene["publication"]["id"]
        assert form_requests == [{"tolerance": CHOSEN}], form_requests
        checks["auto_after_manual"] = {"job_id": automatic, "applied": 0.1, "manual_choice": CHOSEN}
        await applied.scroll_into_view_if_needed()
        await page.set_viewport_size({"width": 790, "height": 930})
        await page.screenshot(path=str(root / "automatic-preview-tolerance.png"))
        (root / "browser-result.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf8")
        await edge.close()


def verify(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-tolerance-browser", app="b03_browser_fixture:app")
    bundle = args.dist.resolve(strict=True)
    (api.root / "b03_browser_fixture.py").write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding="utf8")
    temp = api.root / "browser-tmp"
    temp.mkdir()
    report = {"passed": False, "backend": "fs/sqlite", "model_calls": 0}
    try:
        with api:
            case = api.create_case(CASE)
            dictionary = api.call("GET", api.prefix + "/caseDict")
            settings = {"base": api.base, "case_id": case["id"], "root": str(api.root), "token": api.token,
                        "channel": args.channel}
            if args.fallback_only:
                seeded = api.put(SCRIPT_PATH, FALLBACK_SOURCE + script(FALLBACK_OBJECT))
                job = api.wait_job(seeded["auto_preview_job"])
                assert job["exit_code"] == 0 and "OCP preview fell back to the triangle mesh: ModuleNotFoundError" in job["log_backlog"]
                initial = api.call("GET", api.prefix + "/geometry/scene")
                assert initial["tolerance"] == 0.1 and initial["ocp_status"] == {"status": "fallback", "reason": "ocp_unavailable"}
                settings["fallback_initial_publication"] = initial["publication"]["id"]
                report["fallback_native_seed"] = {"job_id": job["id"], "publication": initial["publication"]["id"]}
            else:
                seeded = api.put(SCRIPT_PATH, script("round_initial"))
                job = api.wait_job(seeded["auto_preview_job"])
                assert "B03_NATIVE_TOLERANCE=0.1" in job["log_backlog"]
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=165,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            report["browser"] = json.loads((api.root / "browser-result.json").read_text(encoding="utf8"))
            assert api.call("GET", api.prefix + "/caseDict") == dictionary
            report["case_dict_and_revision_unchanged"] = True
        report["passed"] = True
    finally:
        (api.root / "geometry-tolerance-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print("GEOMETRY_TOLERANCE_BROWSER_OK", api.root, flush=True)


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
        parser.add_argument("--fallback-only", action="store_true")
        verify(parser.parse_args())
