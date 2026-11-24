"""Isolated browser acceptance for the persisted workbench appearance."""
import argparse
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit


async def browser(data):
    from playwright.async_api import async_playwright, expect

    root, base = Path(data["root"]), data["base"]
    result = {"passed": False, "real_model_requests": 0, "mock_llm_calls": 0, "checks": []}
    reviews = result["checks"]
    theme_text = {"dark": "浅色 SGR", "light": "深色 SGR"}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(channel=data["channel"])
        context = await browser.new_context(viewport={"width": 1660, "height": 1000})
        await context.add_init_script("if(location.origin === " + json.dumps(base) + ") localStorage.setItem('vf_token', " + json.dumps(data["token"]) + ")")
        page = await context.new_page()
        page_errors, external_assets = [], []
        page.on("pageerror", lambda error: page_errors.append(type(error).__name__))

        def record(request):
            if request.resource_type in {"script", "stylesheet", "font"} and urlsplit(request.url).scheme in {"http", "https"}:
                if urlsplit(request.url).netloc != urlsplit(base).netloc:
                    external_assets.append(urlsplit(request.url).hostname)

        page.on("request", record)
        theme = page.get_by_test_id("app-theme")
        html = page.locator("html")

        async def tab(name):
            selector = page.locator(".dv-tab").filter(has_text=re.compile(r"^" + name + r"$"))
            await selector.click(timeout=25000)

        async def colors():
            return await page.evaluate("""() => {
                const body = getComputedStyle(document.body);
                const group = getComputedStyle(document.querySelector('.dv-groupview'));
                return { background: body.backgroundColor, dock: group.getPropertyValue('--dv-background-color').trim() };
            }""")

        async def open_proof():
            folder = page.locator('[data-path="Ref"]')
            await expect(folder).to_be_visible(timeout=20000)
            if await folder.get_attribute("aria-expanded") == "false":
                await folder.click()
            await page.locator('[data-path="Ref/theme-proof.md"]').click(timeout=25000)

        async def check_views(mode):
            await tab("编辑器")
            editor = page.locator(".monaco-editor")
            await expect(editor).to_be_visible(timeout=25000)
            await expect(editor).to_have_class(re.compile(r"(?:^|\s)vs-dark(?:\s|$)" if mode == "dark" else r"(?:^|\s)vs(?:\s|$)"))
            await page.screenshot(path=root / f"monaco-{mode}.png")
            await tab("终端")
            await expect(page.get_by_test_id("terminal-light-sgr")).to_have_text(theme_text[mode])
            await tab("几何预览")
            cad = page.get_by_test_id("geo-canvas-host")
            await expect(cad).to_be_visible(timeout=20000)
            await expect(cad).to_have_attribute("data-theme", mode)
            await expect(cad.locator("canvas")).to_be_visible(timeout=15000)
            await expect(page.get_by_text("theme_box", exact=True)).to_be_visible(timeout=15000)
            await page.screenshot(path=root / f"geometry-{mode}.png")

        try:
            await page.goto(base, wait_until="domcontentloaded")
            await page.locator("[data-case-name=" + json.dumps(data["case"]) + "]").click(timeout=30000)
            await expect(html).to_have_attribute("data-theme", "dark")
            dark = await colors()
            await tab("设置")
            await expect(theme).to_have_value("dark")
            reviews.append("fresh browser settings and root agree on default Dark")

            await theme.select_option("light")
            await expect(html).to_have_attribute("data-theme", "light")
            light = await colors()
            assert all(light[key] != dark[key] for key in dark), (dark, light)
            await open_proof()
            await check_views("light")
            reviews.append("Light reaches root, Dockview, actual local Monaco, xterm and the WebGL CAD canvas")

            await page.reload(wait_until="domcontentloaded")
            await expect(html).to_have_attribute("data-theme", "light")
            await tab("设置")
            await expect(theme).to_have_value("light")
            await theme.select_option("dark")
            await expect(html).to_have_attribute("data-theme", "dark")
            assert await colors() == dark
            await open_proof()
            await check_views("dark")
            await page.reload(wait_until="domcontentloaded")
            await expect(html).to_have_attribute("data-theme", "dark")
            await tab("设置")
            await expect(theme).to_have_value("dark")
            assert external_assets == [] and page_errors == [], (external_assets, page_errors)
            reviews.append("both selections persist across reload; local frontend assets load without script errors")
            result.update(passed=True, browser=browser.version, root_dark=dark, root_light=light, external_asset_requests=0)
        except Exception as error:
            result.update(error=type(error).__name__, detail=str(error)[:1800])
            await page.screenshot(path=root / "theme-failure.png")
            raise
        finally:
            (root / "theme-browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            await browser.close()


def run_case(args, database_url, store):
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    api = IsolatedServer("browser-theme", app="theme_fixture:app", database_url=database_url)
    fixture = "import os\n" + ("os.environ.update(" + repr(dict(store.settings())) + ")\n" if store else "")
    fixture += "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
    fixture += "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n"
    (api.root / "theme_fixture.py").write_text(fixture, encoding="utf8")
    marker = "# Appearance proof\nSame engineering source in both themes.\n"
    with api:
        case = api.create_case("Appearance proof")
        api.put("Ref/theme-proof.md", marker)
        api.put("A-geo/geometry.py", "import cadquery as cq\nshow_object(cq.Workplane('XY').box(2,3,4), name='theme_box')\n")
        job = api.call("POST", api.prefix + "/geometry:preview", {})
        end = time.monotonic() + 150
        while True:
            state = api.call("GET", f"/jobs/{job['job_id']}")
            if state["status"] not in {"queued", "running"}:
                assert state["status"] == "succeeded", state["status"]
                break
            if time.monotonic() >= end:
                raise TimeoutError("Theme scene preview did not stop")
            time.sleep(.35)
        scene = api.call("GET", api.prefix + "/geometry/scene")
        assert any(item["name"] == "theme_box" for item in scene["objects"])
        payload = {"base": api.base, "root": str(api.root), "token": api.token, "case": case["name"], "channel": args.channel}
        subprocess.run([str(args.browser_python), "-B", "-X", "utf8", str(Path(__file__).resolve()), "--browser"],
                       input=json.dumps(payload), text=True, timeout=300, check=True)
        assert api.call("GET", api.prefix + "/file?path=Ref/theme-proof.md")["content"] == marker
        assert api.call("GET", api.prefix + "/geometry/scene") == scene
    assert api.proc.poll() is not None
    print("THEME_BROWSER_OK " + str(api.root), flush=True)


async def run(args):
    from live_llm_terminal_agent_acceptance import disposable_database

    store = nullcontext()
    if args.s3:
        from s3testsupport import ObjectStore
        root = Path(__file__).resolve().parents[2]
        store = ObjectStore(executable=root / "third_party/seaweed-4.52-runtime/weed", volume_max=6)
    async with disposable_database(args.postgres) as database_url:
        with store as active:
            await asyncio.to_thread(run_case, args, database_url, active)
    if args.s3:
        assert active.proc is not None and active.proc.poll() is not None


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--dist", required=True, type=Path)
        parser.add_argument("--browser-python", required=True, type=Path)
        parser.add_argument("--channel", default="msedge")
        parser.add_argument("--postgres", action="store_true")
        parser.add_argument("--s3", action="store_true")
        asyncio.run(run(parser.parse_args()))
