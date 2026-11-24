"""Call the real GeoPanel export control and download native STEP/STL releases."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from urllib.parse import unquote, urlencode, urlsplit


CASE = "One-click CAD export"
PATH = "A-geo/geometry.py"
ORIGINAL = "exported_plate"
RESTORED = "recovered_plate"
FAILURE = "B20_ACTUAL_EXPORT_HOOK_FAILURE"


def source(name, width, *, fail=False):
    contents = ('import cadquery as cq\n'
               'def build_geometry(ctx):\n'
               f'    return {{{name!r}: cq.Workplane("XY").box({width}, 4, 2)}}\n')
    if fail:
        contents += ('def export_geometry(ctx, objects):\n'
                     '    import time\n'
                     '    time.sleep(4)\n'
                     f'    raise RuntimeError({FAILURE!r})\n')
    return contents


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root, base = Path(settings["root"]), settings["base"]
    prefix = f'/api/v1/cases/{settings["case_id"]}'
    headers = {"Authorization": "Bearer " + settings["token"]}
    report = {}
    expect.set_options(timeout=25000)

    def persist():
        (root / "browser-export.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")

    async with async_playwright() as driver:
        edge = await driver.chromium.launch(channel=settings["channel"])
        context = await edge.new_context(viewport={"width": 1490, "height": 1080}, accept_downloads=True)
        await context.add_init_script('if(location.origin===' + json.dumps(base)
            + ')localStorage.setItem("vf_token",' + json.dumps(settings["token"]) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)
        status = page.get_by_test_id("geo-export-status")
        objects = page.get_by_test_id("geo-obj-info")
        export = page.get_by_role("button", name="导出 STEP/STL", exact=True)
        rebuild = page.get_by_role("button", name="重建预览", exact=True)
        published = page.get_by_role("region", name="几何发布文件", exact=True)
        tree_reads = []

        def record_tree(request):
            if request.method == "GET" and urlsplit(request.url).path == prefix + "/tree":
                tree_reads.append(request.url)

        page.on("request", record_tree)

        async def get(path):
            response = await context.request.get(base + path, headers=headers)
            assert response.status == 200, (response.status, await response.text())
            return await response.json()

        async def wait_job(identifier, expected, attempts=55):
            for _ in range(attempts):
                job = await get(f"/api/v1/jobs/{identifier}")
                if job["status"] not in ("running", "queued"):
                    assert job["status"] == expected, job["log_backlog"]
                    assert job["attempt"]["process_stop"]["verified"]
                    return job
                await asyncio.sleep(.75)
            raise TimeoutError(f"Job {identifier} did not end")

        async def click_job(button, route):
            async with page.expect_response(lambda response:
                response.request.method == "POST" and unquote(urlsplit(response.url).path) == prefix + route) as received:
                await button.click()
            response = await received.value
            assert response.status == 200, (response.status, await response.text())
            return (await response.json())["job_id"]

        async def save_source(content):
            prior = await get(prefix + "/file?" + urlencode({"path": PATH}))
            response = await context.request.put(base + prefix + "/file", headers=headers, data={
                "path": PATH, "content": content, "expected_sha256": prior["content_sha256"]})
            assert response.status == 200, (response.status, await response.text())
            ack = await response.json()
            assert ack["ok"] and ack["auto_preview_job"], ack
            await wait_job(ack["auto_preview_job"], "succeeded")

        async def tree_file(name):
            for path in ("A-geo", "A-geo/exports"):
                folder = page.locator(f'[data-path="{path}"]')
                await expect(folder).to_be_visible()
                if await folder.get_attribute("aria-expanded") == "false":
                    await folder.click()
            await expect(page.locator(f'[data-path="A-geo/exports/{name}"]')).to_be_visible()

        async def download_results(exports, tag):
            result = {}
            assert len(exports["files"]) == 2
            for item in exports["files"]:
                name = Path(item["path"]).name
                ext = Path(name).suffix
                async with page.expect_download() as completed:
                    await published.get_by_role("button", name=f"下载 {name}", exact=True).click()
                download = await completed.value
                assert download.suggested_filename == name
                path = root / (tag + "-" + name)
                await download.save_as(path)
                data = path.read_bytes()
                assert len(data) == item["size"] and hashlib.sha256(data).hexdigest() == item["sha256"]
                if ext == ".step":
                    assert data.startswith(b"ISO-10303-21;") and b"END-ISO-10303-21;" in data
                else:
                    binary_count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
                    assert (binary_count and len(data) == 84 + binary_count * 50
                            or data.startswith(b"solid ") and b"facet normal" in data)
                result[ext] = {"file": path.name, "size": len(data), "sha256": item["sha256"]}
            assert set(result) == {".step", ".stl"}
            return result

        await page.goto(base)
        row = page.locator("[data-case-name=" + json.dumps(CASE) + "]")
        if await row.get_attribute("aria-pressed") != "true":
            await row.click()
        if not await export.is_visible():
            await page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^几何预览$")).click()
        await expect(objects).to_contain_text(ORIGINAL)
        starting_reads = len(tree_reads)
        first = await click_job(export, "/geometry:export")
        await expect(export).to_be_disabled()
        await expect(status).to_contain_text(f"STEP/STL 导出完成（作业 #{first}）", timeout=60000)
        await expect(export).to_be_enabled()
        assert len(tree_reads) > starting_reads
        assert (await wait_job(first, "succeeded"))["exit_code"] == 0
        await tree_file(ORIGINAL + ".step")
        await tree_file(ORIGINAL + ".stl")
        await page.get_by_text("已发布的 STEP / STL", exact=True).click()
        previous = await get(prefix + "/geometry/results")
        original_files = await download_results(previous, "first")
        report["one_click"] = {"job": first, "publication": previous["publication"]["id"],
            "downloads": original_files, "tree_refreshed": True}
        persist()

        await save_source(source(ORIGINAL, 3, fail=True))
        broken = await click_job(export, "/geometry:export")
        await expect(status).to_contain_text(f"导出作业 #{broken} 运行中")
        await expect(export).to_be_disabled()
        preview = await click_job(rebuild, "/geometry:preview")
        await expect(status).to_contain_text(f"STEP/STL 导出失败（作业 #{broken}）", timeout=60000)
        await expect(export).to_be_enabled()
        failed = await wait_job(broken, "failed")
        assert FAILURE in failed["log_backlog"] and failed["exit_code"] != 0
        assert (await wait_job(preview, "succeeded"))["exit_code"] == 0
        retained = await get(prefix + "/geometry/results?" + urlencode({"publication_id": previous["publication"]["id"]}))
        assert retained["files"] == previous["files"]
        after_failure = await download_results(retained, "retained")
        assert {ext: item["sha256"] for ext, item in after_failure.items()} == {
            ext: item["sha256"] for ext, item in original_files.items()}
        await status.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / "failed-export-retained.png"))
        report["failed_export"] = {"job": broken, "exit_code": failed["exit_code"], "concurrent_preview": preview,
            "fixed_previous_publication": retained["publication"]["id"], "downloads": after_failure}
        persist()

        await save_source(source(RESTORED, 6))
        another = await click_job(export, "/geometry:export")
        await expect(status).to_contain_text(f"STEP/STL 导出完成（作业 #{another}）", timeout=60000)
        assert (await wait_job(another, "succeeded"))["exit_code"] == 0
        await tree_file(RESTORED + ".step")
        await tree_file(RESTORED + ".stl")
        await expect(published.get_by_role("button", name=f"下载 {ORIGINAL}.step")).to_be_visible()
        await published.get_by_role("button", name="读取最新发布", exact=True).click()
        latest = await get(prefix + "/geometry/results")
        assert latest["publication"]["id"] != previous["publication"]["id"]
        new_files = await download_results(latest, "retry")
        assert all(new_files[ext]["sha256"] != original_files[ext]["sha256"] for ext in new_files)
        report["retry_export"] = {"job": another, "publication": latest["publication"]["id"], "downloads": new_files}
        await status.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / "retry-export.png"))
        persist()
        await edge.close()


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-export-browser", app="b20_browser_fixture:app")
    root = api.root
    bundle = args.dist.resolve(strict=True)
    (root / "b20_browser_fixture.py").write_text(
        "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(bundle)) + ", html=True))\n", encoding="utf8")
    temp = root / "browser-tmp"
    temp.mkdir()
    report = {"passed": False, "model_calls": 0, "backend": "fs/sqlite"}
    print("GEOMETRY_EXPORT_BROWSER_EVIDENCE", root, flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            prefix = api.prefix
            seeded = api.put(PATH, source(ORIGINAL, 3))
            assert api.wait_job(seeded["auto_preview_job"])["exit_code"] == 0
            settings = {"base": api.base, "case_id": case["id"], "root": str(root), "token": api.token,
                        "channel": args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=240,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            latest = api.call("GET", prefix + "/geometry/results")
            query = urlencode({"publication_id": latest["publication"]["id"], "path": latest["files"][0]["path"]})
            api.create_case("Other CAD export account")
            api.call("GET", prefix + "/geometry/results", expect=404)
            api.call("GET", prefix + "/geometry/artifact?" + query, expect=404)
            report["other_account_404"] = 2
        report["passed"] = True
    finally:
        observed = root / "browser-export.json"
        if observed.is_file():
            report["browser"] = json.loads(observed.read_text(encoding="utf8"))
        (root / "geometry-export-result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf8")
    print("GEOMETRY_EXPORT_BROWSER_OK", root, flush=True)


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
