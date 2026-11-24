"""Real browser and PTY acceptance for terminal selection preview. No model calls."""
import argparse
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = r"""# Preview heading

**Terminal document** with inline $x^2$ and \(a+b\).

\[\sqrt{x^2+1}\]

| Step | Status |
|---|---|
| Mesh | ready |

```sh
echo '$literal$'
```

[Reference](https://preview.invalid/never)
![Reference image](https://preview.invalid/asset.png)
<img src="https://preview.invalid/html.png">

END_PREVIEW
"""
FORMULA = r"\frac{1}{\sqrt{2}}"


async def browser_check(data):
    from playwright.async_api import async_playwright, expect

    base = data["base"]
    root = Path(data["root"])
    headers = {"Authorization": "Bearer " + data["token"]}
    result = {"passed": False, "real_model_requests": 0, "mock_llm_calls": 0, "checks": []}
    checks = result["checks"]
    sessions, requests, terminal_inputs, terminal_events = [], [], [], []

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(channel=data["channel"])
        context = await browser.new_context(viewport={"width": 1536, "height": 1100}, locale="zh-CN")
        if data["clipboard_links"]:
            await context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
        page = await context.new_page()
        await page.add_init_script("localStorage.setItem('vf_token', " + json.dumps(data["token"]) + ");"
                                   "localStorage.setItem('vf_theme', " + json.dumps(data["theme"]) + ");")
        cdp = await context.new_cdp_session(page)

        def observe_socket(socket):
            if urlsplit(socket.url).path != "/ws/terminal":
                return

            def sent(frame):
                if isinstance(frame, str) and json.loads(frame).get("type") == "input":
                    terminal_inputs.append(frame)

            socket.on("framesent", sent)
            socket.on("framereceived", lambda frame: terminal_events.append(json.loads(frame)) if isinstance(frame, str) else None)

        def observe_request(request):
            url = urlsplit(request.url)
            if url.hostname == "preview.invalid" or (request.method == "POST" and
                    re.search(r"/(?:conversations/\d+/messages|turns)$", url.path)):
                requests.append(url.path)

        page.on("websocket", observe_socket)
        page.on("request", observe_request)
        terminal = page.get_by_test_id("terminal-panel")
        rows = terminal.locator(".xterm-rows")
        entry = terminal.locator(".xterm-helper-textarea")
        preview_button = terminal.get_by_test_id("terminal-preview-selection")
        dialog = page.get_by_test_id("terminal-selection-preview")
        output = page.get_by_test_id("terminal-preview-content")
        tabs = terminal.locator(".terminal-session-tab")

        async def select_case(name):
            await page.locator("[data-case-name=" + json.dumps(name) + "]").click(timeout=30000)

        async def command(text):
            await entry.focus()
            await cdp.send("Input.insertText", {"text": text})
            await page.keyboard.press("Enter")

        async def new_terminal(shell, *, bound=True):
            await terminal.get_by_role("button", name="新建终端", exact=True).click()
            row = tabs.filter(has_text=re.compile(r"^" + shell + r"\s*·"))
            await expect(row).to_contain_text("运行中", timeout=90000)
            if bound:
                await expect(terminal.get_by_test_id("terminal-to-agent")).to_be_enabled(timeout=30000)
            identifier = await row.get_attribute("data-session-id")
            assert identifier
            sessions.append(identifier)
            return row

        async def close_terminal(identifier):
            path = base + "/api/v1/terminal/sessions/" + identifier
            outsider = await context.request.get(path, headers={"Authorization": "Bearer " + data["outsider"]})
            assert outsider.status == 404
            await terminal.locator(f'.terminal-session-tab[data-session-id="{identifier}"]').get_by_role(
                "button", name=re.compile("^关闭终端")).click(timeout=25000)
            async with asyncio.timeout(60):
                while True:
                    state = await (await context.request.get(path, headers=headers)).json()
                    if state.get("phase") == "closed":
                        assert state["cleanup_verified"] and state["stop"]["verified"], state
                        return state
                    await asyncio.sleep(.2)

        async def select_rows(start, end=None):
            first = rows.get_by_text(start, exact=True)
            last = rows.get_by_text(end or start, exact=True)
            await expect(first).to_be_visible(timeout=15000)
            await expect(last).to_be_visible(timeout=15000)
            left, right = await first.bounding_box(), await last.bounding_box()
            assert left and right
            start_point = [left["x"] + 1, left["y"] + left["height"] / 2]
            end_point = [right["x"] + right["width"] + 1, right["y"] + right["height"] / 2]
            for point in [start_point, end_point]:
                assert await page.evaluate("([x,y]) => !!document.elementFromPoint(x,y)?.closest('.xterm-screen')", point), point
            await page.mouse.move(*start_point)
            await page.mouse.down()
            await page.mouse.move(*end_point, steps=15)
            await page.mouse.up()
            await expect(preview_button).to_be_enabled()

        try:
            await page.goto(base, wait_until="domcontentloaded")
            tab = page.locator(".dv-tab").filter(has_text=re.compile("^终端$"))
            if not data["clipboard_links"]:
                await select_case(data["case"])
            await tab.click()
            box = await tab.bounding_box()
            assert box
            await page.mouse.move(768, box["y"])
            await page.mouse.down()
            await page.mouse.move(768, box["y"] - 340, steps=16)
            await page.mouse.up()

            if data["clipboard_links"]:
                standalone = await new_terminal("cmd", bound=False)
                await expect(rows).to_contain_text(re.compile(r"[A-Za-z]:\\[^>]+>"), timeout=20000)
                await command("cls & echo PREVIEW_WITHOUT_CASE")
                await select_rows("PREVIEW_WITHOUT_CASE")
                await expect(terminal.get_by_test_id("terminal-to-agent")).to_be_disabled()
                await preview_button.click()
                await expect(output).to_contain_text("PREVIEW_WITHOUT_CASE")
                await dialog.get_by_role("button", name="关闭预览").click()
                identifier = await standalone.get_attribute("data-session-id")
                state = await close_terminal(identifier)
                assert state['case_id'] is None
                sessions.remove(identifier)
                await select_case(data["case"])
                await tab.click()
                checks.append("standalone terminal selection preview works in its own authorized session")

            await expect(preview_button).to_be_disabled()
            cmd = await new_terminal("cmd")
            await expect(rows).to_contain_text(re.compile(r"[A-Za-z]:\\[^>]+>"), timeout=20000)
            await command("echo CMD_ACTIVE_MARKER")
            await expect(rows).to_contain_text("CMD_ACTIVE_MARKER", timeout=20000)
            await terminal.get_by_role("button", name="WSL", exact=True).click()
            await expect(cmd.get_by_role("button").first).to_have_attribute("aria-pressed", "true")
            wsl = await new_terminal("wsl")
            await expect(cmd).to_contain_text("运行中")
            await expect(rows).to_contain_text("nobody@", timeout=25000)
            await expect(preview_button).to_be_disabled()
            await command("stty -echo; clear")
            if data["clipboard_links"]:
                await command("printf '%s\\n' TERMINAL_CLIPBOARD_SOURCE")
                await select_rows("TERMINAL_CLIPBOARD_SOURCE")
                assert await page.evaluate("document.execCommand('copy')")
                assert (await page.evaluate("navigator.clipboard.readText()")).strip() == "TERMINAL_CLIPBOARD_SOURCE"
                await page.evaluate("navigator.clipboard.writeText(\"printf '%s\\\\n' TERMINAL_PASTE_RESULT\")")
                assert "TERMINAL_PASTE_RESULT" in await page.evaluate("navigator.clipboard.readText()")
                await entry.focus()
                await page.keyboard.press("Control+Shift+V")
                async with asyncio.timeout(6):
                    while not any("TERMINAL_PASTE_RESULT" in frame for frame in terminal_inputs):
                        await asyncio.sleep(.05)
                await page.keyboard.press("Enter")
                await expect(rows.get_by_text("TERMINAL_PASTE_RESULT", exact=True)).to_be_visible(timeout=12000)

                link = base + "/api/health"
                await command("printf '%s\\n' " + link)
                url_cell = rows.get_by_text(link, exact=True)
                await expect(url_cell).to_be_visible(timeout=12000)
                link_box = await url_cell.bounding_box()
                assert link_box
                point = (link_box["x"] + link_box["width"] / 2, link_box["y"] + link_box["height"] / 2)
                await page.mouse.move(*point)
                await page.wait_for_function("!!document.querySelector('.xterm-cursor-pointer')", timeout=7000)
                async with context.expect_page(timeout=10000) as opened:
                    await page.mouse.click(*point)
                target = await opened.value
                await target.wait_for_url(link, timeout=10000)
                await target.close()

                previous = [event for event in terminal_events if event.get("type") == "resize_accepted"]
                await page.set_viewport_size({"width": 1360, "height": 1100})
                async with asyncio.timeout(15):
                    while True:
                        resized = [event for event in terminal_events if event.get("type") == "resize_accepted"]
                        if len(resized) > len(previous) and (not previous or resized[-1]["cols"] < previous[-1]["cols"]):
                            result["verified_resize_cols"] = resized[-1]["cols"]
                            break
                        await asyncio.sleep(.1)
                await page.set_viewport_size({"width": 1536, "height": 1100})
                checks.append("actual clipboard copy and paste, local WebLinks navigation and PTY resize acknowledgment")
            await command("clear; python3 Ref/preview-proof.py document")
            await select_rows("# Preview heading", "END_PREVIEW")
            await preview_button.click()
            await expect(dialog).to_be_visible()
            await expect(output.get_by_role("heading", name="Preview heading")).to_be_visible()
            await expect(output.locator("strong")).to_have_text("Terminal document")
            await expect(output.get_by_role("cell", name="Mesh")).to_be_visible()
            await expect(output.locator(".katex")).to_have_count(3)
            await expect(output.locator(".katex-display svg")).to_have_count(1)
            await expect(output.locator("pre code")).to_contain_text("$literal$")
            await expect(output.locator("a, img, [src], [href]")).to_have_count(0)
            await expect(output).to_contain_text("Reference image")
            await page.screenshot(path=root / "markdown-preview.png")
            checks.append("actual multi-line WSL selection renders Markdown, table, delimiters, and safe literal code")

            await dialog.get_by_role("button", name="LaTeX").click()
            await expect(dialog.get_by_role("status")).to_contain_text("无法渲染该公式")
            await expect(output.locator("pre")).to_contain_text("# Preview heading")
            input_before = list(terminal_inputs)
            await page.keyboard.press("Escape")
            await expect(dialog).to_have_count(0)
            await expect(preview_button).to_be_focused()
            assert input_before == terminal_inputs
            checks.append("invalid formula visibly falls back and Escape returns focus without a PTY write")

            await command("clear; python3 Ref/preview-proof.py formula")
            await select_rows(FORMULA)
            await preview_button.click()
            await dialog.get_by_role("button", name="LaTeX").click()
            await expect(output.locator(".katex .mfrac")).to_have_count(1)
            await expect(output.locator(".katex svg")).to_have_count(1)
            await page.set_viewport_size({"width": 390, "height": 780})
            await expect(dialog).to_be_visible()
            rect = await dialog.bounding_box()
            assert rect and rect["width"] <= 390 and rect["height"] <= 780
            await page.screenshot(path=root / "formula-narrow.png")
            await page.set_viewport_size({"width": 1536, "height": 1100})
            await dialog.get_by_role("button", name="关闭预览").click()
            await expect(dialog).to_have_count(0)
            checks.append("bare LaTeX renders a fraction and radical within the 390 px viewport")

            await cmd.get_by_role("button").first.click()
            await expect(rows).to_contain_text("CMD_ACTIVE_MARKER", timeout=20000)
            await expect(preview_button).to_be_disabled()
            await command("echo CMD_AFTER_SWITCH")
            await expect(rows).to_contain_text("CMD_AFTER_SWITCH", timeout=15000)
            await wsl.get_by_role("button").first.click()
            await expect(rows.get_by_text(FORMULA, exact=True)).to_be_visible(timeout=20000)
            await expect(preview_button).to_be_disabled()
            checks.append("cmd and WSL coexist with independent backlog, focused input and selection")

            await select_case(data["other_case"])
            await expect(tabs).to_have_count(0, timeout=25000)
            await expect(preview_button).to_be_disabled()
            await expect(dialog).to_have_count(0)
            await select_case(data["case"])
            await expect(tabs).to_have_count(2, timeout=25000)
            await expect(preview_button).to_be_disabled()
            for identifier in sessions:
                await close_terminal(identifier)
            checks.append("case switch and another account cannot access the selection or terminal; both native stops verified")
            assert requests == [], requests
            result.update(passed=True, browser=browser.version, theme=data["theme"], sessions=sessions, external_preview_requests=0)
        except Exception as error:
            result.update(error=type(error).__name__, detail=str(error)[:2500])
            await page.screenshot(path=root / "preview-failure.png")
            for identifier in sessions:
                await context.request.delete(base + "/api/v1/terminal/sessions/" + identifier, headers=headers, timeout=15000)
            raise
        finally:
            (root / "terminal-preview-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            await browser.close()


def run_case(args, database_url, store):
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    fixture = "import os\n" + ("os.environ.update(" + repr(dict(store.settings())) + ")\n" if store else "")
    fixture += "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
    fixture += "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n"
    api = IsolatedServer("terminal-preview", app="preview_fixture:app", database_url=database_url)
    (api.root / "preview_fixture.py").write_text(fixture, encoding="utf8")
    with api:
        case = api.create_case("Terminal Preview Example")
        workspace = api.call("GET", "/workspaces")[0]
        other = api.call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "Other Preview Case"})
        visitor = api.call("POST", "/auth/register", {"email": "preview-outsider@example.com", "name": "Preview visitor",
                                                        "password": "isolated-preview-234"})
        api.put("Ref/preview-proof.py", "import sys\nprint(" + repr({"document": DOCUMENT, "formula": FORMULA}) + "[sys.argv[1]])\n")
        data = {"base": api.base, "root": str(api.root), "token": api.token, "outsider": visitor["token"],
                "case": case["name"], "other_case": other["name"], "theme": args.theme, "channel": args.channel,
                "clipboard_links": args.clipboard_links}
        subprocess.run([str(args.browser_python), "-B", "-X", "utf8", str(Path(__file__).resolve()), "--browser"],
                       input=json.dumps(data), text=True, timeout=390, check=True)
    assert api.proc.poll() is not None
    print("TERMINAL_PREVIEW_OK " + str(api.root), flush=True)


async def run(args):
    from live_llm_terminal_agent_acceptance import disposable_database

    store = nullcontext()
    if args.s3:
        from s3testsupport import ObjectStore
        store = ObjectStore(executable=ROOT / "third_party/seaweed-4.52-runtime/weed", volume_max=6)
    async with disposable_database(args.postgres) as database_url:
        with store as active:
            await asyncio.to_thread(run_case, args, database_url, active)
    if args.s3:
        assert active.proc is not None and active.proc.poll() is not None


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        asyncio.run(browser_check(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--dist", type=Path, required=True)
        parser.add_argument("--browser-python", type=Path, required=True)
        parser.add_argument("--channel", default="msedge")
        parser.add_argument("--theme", default="dark", choices=("dark", "light"))
        parser.add_argument("--postgres", action="store_true")
        parser.add_argument("--s3", action="store_true")
        parser.add_argument("--clipboard-links", action="store_true")
        asyncio.run(run(parser.parse_args()))
