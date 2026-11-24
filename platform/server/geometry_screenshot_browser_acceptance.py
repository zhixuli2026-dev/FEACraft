"""Exercise a real CAD canvas download and its local screenshot message contract."""

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


CASE = "CAD screenshot"
EMPTY = "Empty CAD screenshot case"
SOURCE = '''import cadquery as cq

def build_geometry(ctx):
    return {"capture_plate": cq.Workplane("XY").box(12, 8, 3).faces(">Z").workplane().hole(3)}
'''

REQUEST = r"""
({filename, mode, caseName}) => new Promise((resolve) => {
  const done = (reply) => {
    clearTimeout(timer);
    removeEventListener("message", listen);
    resolve(reply);
  };
  const listen = (event) => {
    if (event.source === window && event.origin === location.origin &&
        event.data?.command === "screenshot" && event.data.text?.filename === filename) {
      done(event.data.text);
    }
  };
  const timer = setTimeout(() => done({timeout: true}), 4000);
  addEventListener("message", listen);
  const request = { type: "screenshot", filename };
  if (mode === "switch") {
    document.querySelector("[data-case-name='" + caseName + "']").click();
  }
  if (mode === "standard") window.postMessage(JSON.stringify(request), location.origin);
  else window.dispatchEvent(new MessageEvent("message", {data: request}));
})
"""

FRAME_PROBE = r"""
(filename) => new Promise((resolve) => {
  const frame = document.createElement("iframe");
  frame.sandbox = "allow-scripts";
  let delivered = false;
  let receivedImage = false;
  const listen = (event) => {
    if (event.source === frame.contentWindow && event.origin === "null" &&
        event.data?.type === "frame-delivered") delivered = true;
    if (event.data?.command === "screenshot" && event.data.text?.filename === filename) {
      receivedImage = true;
    }
  };
  addEventListener("message", listen);
  frame.srcdoc = "<script>parent.postMessage(" + JSON.stringify({type: "screenshot", filename}) +
    ",'*');parent.postMessage({type: 'frame-delivered'},'*');</script>";
  frame.hidden = true;
  document.body.append(frame);
  setTimeout(() => {
    removeEventListener("message", listen);
    frame.remove();
    resolve({delivered, receivedImage});
  }, 1000);
})
"""


async def browser(settings):
    from PIL import Image, ImageChops
    from playwright.async_api import async_playwright, expect

    base, root = settings["base"], Path(settings["root"])
    prefix = f'/api/v1/cases/{settings["case_id"]}'
    headers = {"Authorization": "Bearer " + settings["token"]}
    expect.set_options(timeout=25000)

    def inspect(data, expected_size):
        with Image.open(BytesIO(data)) as image:
            assert image.format == "PNG" and image.size == expected_size, (image.format, image.size, expected_size)
            return image.convert("RGB")

    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1490, "height": 1060}, device_scale_factor=1.25,
                                         accept_downloads=True)
        await context.add_init_script('if(location.origin===' + json.dumps(base)
            + ')localStorage.setItem("vf_token",' + json.dumps(settings["token"]) + ');')
        page = await context.new_page()
        canvas = page.get_by_test_id("geo-canvas-host").locator("canvas")
        toolbar = page.locator("[data-auto-job]")
        obj = page.get_by_test_id("geo-obj-info")
        status = page.get_by_test_id("geo-status")

        async def choose(name):
            row = page.locator("[data-case-name=" + json.dumps(name) + "]")
            if await row.get_attribute("aria-pressed") != "true":
                await row.click()
            await expect(row).to_have_attribute("aria-pressed", "true")

        async def settle_frame():
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

        async def request(filename, mode="local", case_name=None):
            reply = await page.evaluate(REQUEST, {"filename": filename, "mode": mode, "caseName": case_name})
            assert set(reply) == {"filename", "data"} and reply["filename"] == filename, reply.keys()
            return reply["data"]

        async def capture(filename, mode="local"):
            await settle_frame()
            size = tuple(await canvas.evaluate("(canvas) => [canvas.width, canvas.height]"))
            data = await request(filename, mode)
            assert isinstance(data, str) and data.startswith("data:image/png;base64,")
            content = base64.b64decode(data.partition(",")[2], validate=True)
            return inspect(content, size), content

        await page.goto(base)
        await choose(CASE)
        if not await toolbar.is_visible():
            await page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^几何预览$")).click()
        await expect(obj).to_contain_text("capture_plate")
        await expect(canvas).to_be_visible()
        full, message_png = await capture("回传-几何.png", "standard")
        (root / "message-capture.png").write_bytes(message_png)
        old_request, _ = await capture("原同页事件.png")
        assert ImageChops.difference(full, old_request).getbbox() is None

        probe = await page.evaluate(FRAME_PROBE, "different-origin-frame.png")
        assert probe == {"delivered": True, "receivedImage": False}, probe

        checkbox = page.get_by_role("checkbox", name="显示 capture_plate", exact=True)
        await expect(checkbox).to_be_checked()
        await checkbox.uncheck()
        hidden, _ = await capture("隐藏对象.png")
        assert full.size == hidden.size
        difference = ImageChops.difference(full, hidden).getbbox()
        assert difference and (difference[2] - difference[0]) * (difference[3] - difference[1]) > 500
        await checkbox.check()

        await toolbar.get_by_text("视角与截图", exact=True).click()
        button = toolbar.get_by_role("button", name="截图", exact=True)
        await expect(button).to_be_visible()
        await settle_frame()
        download_size = tuple(await canvas.evaluate("(canvas) => [canvas.width, canvas.height]"))
        async with page.expect_download() as completed:
            await button.click()
        download = await completed.value
        assert download.suggested_filename == CASE + "-cad.png"
        saved_path = root / "downloaded-cad.png"
        await download.save_as(saved_path)
        downloaded = inspect(saved_path.read_bytes(), download_size)
        await expect(status).to_contain_text("已截取 CAD 视图")

        await button.evaluate("""(button) => {
            const canvas = document.querySelector('[data-testid="geo-canvas-host"] canvas');
            const width = canvas.width;
            try { canvas.width = 0; button.click(); }
            finally { canvas.width = width; }
        }""")
        await expect(status).to_contain_text("未能截取 CAD 视图")
        recovered, _ = await capture("同算例恢复.png")
        assert recovered.getbbox()

        assert await request("切换中的响应.png", "switch", EMPTY) is None
        await choose(EMPTY)
        await expect(obj).not_to_contain_text("capture_plate")
        assert await request("空算例响应.png", "standard") is None
        await choose(CASE)
        await expect(obj).to_contain_text("capture_plate")
        final, recovered_bytes = await capture("切回恢复.png", "standard")
        assert final.getbbox()
        (root / "restored-cad.png").write_bytes(recovered_bytes)

        response = await context.request.get(base + prefix + "/geometry/scene", headers=headers)
        assert response.status == 200 and await response.json() == settings["scene"]
        evidence = {"message_png_dimensions": list(full.size), "downloaded_png_dimensions": list(downloaded.size),
            "native_download_filename": download.suggested_filename, "png_sha256": hashlib.sha256(message_png).hexdigest(),
            "visible_object_pixel_difference_bbox": list(difference), "filename_round_trip": True,
            "original_and_standard_events_agree": True, "frame_message_rejected": True,
            "recovered_zero_width_capture": True, "cross_case_previous_frame_rejected": True,
            "restored_original_case_capture": True, "unchanged_scene_publication": settings["scene"]["publication"]["id"]}
        (root / "browser-screenshot.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf8")
        await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-screenshot-browser", app="b14_browser_fixture:app")
    root = api.root
    bundle = args.dist.resolve(strict=True)
    (root / "b14_browser_fixture.py").write_text(
        "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(bundle)) + ", html=True))\n", encoding="utf8")
    temp = root / "browser-tmp"
    temp.mkdir()
    report = {"passed": False, "model_calls": 0, "backend": "fs/sqlite"}
    print("GEOMETRY_SCREENSHOT_BROWSER_EVIDENCE", root, flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            prefix = api.prefix
            workspace = api.call("GET", prefix)["workspace_id"]
            api.call("POST", f"/workspaces/{workspace}/cases", {"name": EMPTY})
            saved = api.put("A-geo/geometry.py", SOURCE)
            assert api.wait_job(saved["auto_preview_job"])["exit_code"] == 0
            scene = api.call("GET", prefix + "/geometry/scene")
            settings = {"base": api.base, "case_id": case["id"], "root": str(root), "token": api.token,
                        "channel": args.channel, "scene": scene}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=120,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            api.create_case("Other screenshot account")
            api.call("GET", prefix + "/geometry/scene", expect=404)
            report["other_account_404"] = True
        report["passed"] = True
    finally:
        observed = root / "browser-screenshot.json"
        if observed.is_file():
            report["browser"] = json.loads(observed.read_text(encoding="utf8"))
        (root / "geometry-screenshot-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print("GEOMETRY_SCREENSHOT_BROWSER_OK", root, flush=True)


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
