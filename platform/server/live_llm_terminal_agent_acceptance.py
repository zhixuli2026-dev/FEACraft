"""Exercise terminal-to-Agent draft transfer and optional real vision in a disposable case."""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager, nullcontext
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import urlencode, urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "platform/server"
sys.path.insert(0, str(SERVER))
COLOR = (31, 147, 83)
SELECTED = "EXACT_SELECTED_TERMINAL_TEXT"
INITIAL = "Keep this unsent draft"
PROMPT = "请只根据终端截图读出 VISION CODE: 后的八位数字，直接回复这八位数字。"
VISION_OUTBOUND_OBSERVER = """
import base64,hashlib
send_with_receipt=httpx.AsyncClient.send
async def observe_image(self,request,*args,**kwargs):
 if request.url.host=='api.deepseek.com' and request.url.path.endswith('/chat/completions'):
  messages=json.loads(request.content)['messages']
  payloads=[part for message in messages if isinstance(message.get('content'),list)
   for part in message['content'] if part.get('type')=='image_url']
  digests=[hashlib.sha256(base64.b64decode(part['image_url']['url'].split(',',1)[1])).hexdigest()
   for part in payloads if part['image_url']['url'].startswith('data:image/png;base64,')]
  texts=[message['content'] for message in messages if isinstance(message.get('content'),str)]
  texts += [part.get('text','') for message in messages if isinstance(message.get('content'),list)
   for part in message['content'] if part.get('type')=='text']
  nonce=(root/'expected-nonce.txt').read_text()
  receipt({'kind':'vision_input','image_digests':digests,
   'nonce_in_text':any(nonce in value for value in texts),
   'nonce_in_tool_result':any(nonce in json.dumps(message.get('content')) for message in messages if message.get('role')=='tool'),
   'tool_results':sum(message.get('role')=='tool' for message in messages)})
 return await send_with_receipt(self,request,*args,**kwargs)
httpx.AsyncClient.send=observe_image
"""


async def browser_check(data):
    from PIL import Image
    from playwright.async_api import async_playwright, expect

    output = Path(data["root"])
    base = data["base"]
    headers = {"Authorization": "Bearer " + data["token"]}
    checks = []
    events = []
    frames = []
    submissions = []
    attachment_bytes = None
    session_id = None
    report = {"checks": checks, "draft_model_requests": 0}

    async with async_playwright() as play:
        browser = await play.chromium.launch(channel=data["channel"] or None)
        context = await browser.new_context(viewport={"width": 1536, "height": 1100}, locale="zh-CN")
        page = await context.new_page()
        await page.add_init_script(
            "if(location.origin === " + json.dumps(base) + " && !localStorage.getItem('vf_token')) "
            "localStorage.setItem('vf_token', " + json.dumps(data["token"]) + ");"
        )

        def observe(socket):
            if urlsplit(socket.url).path != "/ws/terminal":
                return

            def from_shell(payload):
                if isinstance(payload, bytes):
                    frames.append(payload)
                else:
                    events.append(json.loads(payload))

            socket.on("framereceived", from_shell)

        def record_request(request):
            if request.method == "POST" and (
                re.search(r"/conversations/\d+/messages$", urlsplit(request.url).path)
                or "/turns" in urlsplit(request.url).path
            ):
                submissions.append(request.post_data_json)

        page.on("websocket", observe)
        page.on("request", record_request)
        terminal = page.get_by_test_id("terminal-panel")
        entry = terminal.locator(".xterm-helper-textarea")
        rows = terminal.locator(".xterm-rows")
        viewer = terminal.locator(".xterm-screen")
        screenshot = terminal.get_by_test_id("terminal-screenshot-to-agent")
        transfer = terminal.get_by_test_id("terminal-to-agent")
        chips = page.get_by_test_id("attachment-chip")
        composer = page.get_by_test_id("agent-input")
        backend = page.get_by_test_id("agent-backend")
        cdp = await context.new_cdp_session(page)

        async def tab(name):
            await page.locator(".dv-tab").filter(has_text=re.compile("^" + name + "$")).click(timeout=25000)

        async def select_case(name):
            await page.locator("[data-case-name=" + json.dumps(name) + "]").click(timeout=30000)

        async def command(text):
            await entry.focus()
            await cdp.send("Input.insertText", {"text": text})
            await page.keyboard.press("Enter")

        async def ready():
            async with asyncio.timeout(75):
                while not any(event["type"] == "ready" for event in events):
                    assert not any(event["type"] == "error" for event in events), events
                    await asyncio.sleep(.05)
            return next(event["session"] for event in events if event["type"] == "ready")

        async def load_attachment(locator):
            await expect(locator).to_be_visible(timeout=20000)
            async with asyncio.timeout(20):
                while not await locator.evaluate("img => img.complete && img.naturalWidth > 0"):
                    await asyncio.sleep(.05)
            image_url = await locator.get_attribute("src")
            assert image_url.startswith("data:image/png;base64,")
            return base64.b64decode(image_url.split(",", 1)[1], validate=True)

        async def capture():
            await tab("终端")
            await expect(screenshot).to_be_enabled(timeout=20000)
            await screenshot.click()

        async def enlarge_terminal():
            terminal_tab = page.locator(".dv-tab").filter(has_text=re.compile("^终端$"))
            box = await terminal_tab.bounding_box()
            assert box, "Terminal tab is missing"
            center = page.viewport_size["width"] / 2
            await page.mouse.move(center, box["y"])
            await page.mouse.down()
            await page.mouse.move(center, box["y"] - 275, steps=16)
            await page.mouse.up()
            resized = await terminal_tab.bounding_box()
            assert resized and resized["y"] < box["y"] - 200, (box, resized)

        async def click_rendered_cell(locator, count):
            box = await locator.bounding_box()
            assert box and box["width"] > 100, box
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            is_terminal = await page.evaluate("([x, y]) => !!document.elementFromPoint(x,y)?.closest('.xterm-screen')", [x, y])
            assert is_terminal, (x, y, box)
            await page.mouse.click(x, y, click_count=count)

        try:
            await page.goto(base, wait_until="domcontentloaded")
            await select_case(data["case_name"])
            await tab("Agent")
            await composer.fill(INITIAL)
            await tab("终端")
            await enlarge_terminal()
            await expect(transfer).to_be_disabled()
            await expect(screenshot).to_be_disabled()
            await terminal.get_by_role("button", name="WSL", exact=True).click()
            await terminal.get_by_role("button", name="新建终端", exact=True).click()
            session_id = await ready()
            await expect(rows).to_contain_text("nobody@", timeout=25000)
            await command("stty -echo; clear; python3 Ref/terminal-capture.py " + data["nonce"])
            await expect(rows).to_contain_text(data["nonce"], timeout=20000)
            assert data["nonce"].encode() in b"".join(frames)
            checks.append("real WSL nobody, and transfer controls unavailable before readiness")

            span = rows.get_by_text(SELECTED, exact=True)
            await click_rendered_cell(span, 2)
            selection = terminal.locator(".xterm-selection > div")
            await expect(selection.first).to_be_visible()
            checks.append("real xterm pointer selection overlay rendered before transfer")
            await transfer.click()
            await expect(composer).to_have_value(INITIAL + "\n\n" + SELECTED)
            checks.append("mouse-selected PTY text is appended verbatim to existing draft")

            await tab("终端")
            await click_rendered_cell(rows.get_by_text(SELECTED, exact=True), 1)
            await expect(selection).to_have_count(0)
            await transfer.click()
            await expect(composer).to_have_value(re.compile("RECENT_TERMINAL_LINE_22"))
            draft = await composer.input_value()
            assert "HIDDEN_SCROLLBACK_SENTINEL" not in draft and "RECENT_TERMINAL_LINE_03" not in draft, draft
            checks.append("unselected output joins only the recent 20 buffer lines")

            await tab("终端")
            await command("clear; python3 Ref/terminal-capture.py " + data["nonce"] + " screenshot")
            await expect(rows).to_contain_text("SCREENSHOT WINDOW", timeout=15000)
            await expect(rows).to_contain_text(data["nonce"], timeout=15000)
            extent = await viewer.bounding_box()
            assert extent
            await terminal.screenshot(path=output / "terminal-source.png")
            await screenshot.click()
            await expect(chips).to_have_count(1)
            preview = chips.locator("img")
            await expect(preview).to_have_attribute("alt", re.compile(r"^terminal-.*\.png$"))
            attachment_bytes = await load_attachment(preview)
            image = Image.open(io.BytesIO(attachment_bytes)).convert("RGB")
            assert len(attachment_bytes) <= 4 * 1024 * 1024 and max(image.size) <= 2048, image.size
            assert abs(image.width / image.height - extent["width"] / extent["height"]) < .05, (image.size, extent)
            color_pixels = sum(1 for pixel in image.getdata() if pixel == COLOR)
            assert color_pixels >= 100, color_pixels
            (output / "terminal-attachment.png").write_bytes(attachment_bytes)
            await expect(composer).to_have_value(draft)
            await page.screenshot(path=output / "agent-draft.png")
            report["image"] = {"size": len(attachment_bytes), "dimensions": image.size, "ansi_color_pixels": color_pixels,
                               "sha256": hashlib.sha256(attachment_bytes).hexdigest()}
            checks.append("real visible terminal PNG matches viewport ratio and rendered ANSI background")

            await backend.select_option("wsl")
            await expect(page.get_by_test_id("native-input")).to_have_value(draft)
            await expect(chips).to_have_count(1)
            assert (await load_attachment(chips.locator("img"))) == attachment_bytes
            await backend.select_option("api")
            await expect(composer).to_have_value(draft)
            checks.append("same text and PNG enter API Agent and native Codex composer without dispatch")

            await capture()
            await expect(chips).to_have_count(2)
            await capture()
            await expect(chips).to_have_count(3)
            await capture()
            await expect(terminal.get_by_role("alert").filter(has_text="最多 3 张")).to_be_visible()
            await tab("Agent")
            await expect(chips).to_have_count(3)
            await chips.get_by_test_id("attachment-remove").last.click()
            await chips.get_by_test_id("attachment-remove").last.click()
            await expect(chips).to_have_count(1)
            checks.append("fourth image refused, existing drafts retained, and images removable")

            await tab("终端")
            await page.evaluate("""() => {
              const original = HTMLCanvasElement.prototype.toBlob;
              HTMLCanvasElement.prototype.toBlob = function(callback, ...args) {
                HTMLCanvasElement.prototype.toBlob = original;
                return original.call(this, blob => window.finishTerminalCapture = () => {
                  delete window.finishTerminalCapture;
                  callback(blob);
                }, ...args);
              };
            }""")
            await screenshot.click()
            await page.wait_for_function("typeof window.finishTerminalCapture === 'function'")
            await select_case(data["other_case"])
            await tab("Agent")
            await expect(composer).to_have_value("", timeout=25000)
            await expect(chips).to_have_count(0)
            await page.evaluate("window.finishTerminalCapture()")
            await select_case(data["case_name"])
            await tab("Agent")
            await expect(composer).to_have_value(draft, timeout=25000)
            await expect(chips).to_have_count(1)
            checks.append("case switch isolates drafts and drops delayed screenshot encoding")
            assert submissions == [], submissions

            if data["live_vision"]:
                await composer.fill(PROMPT)
                send = page.locator(".agent-panel").get_by_role("button", name="发送", exact=True)
                message_path = f"/api/v1/conversations/{data['conversation_id']}/messages"
                rejected = 0
                if data.get("owner_lock_rejection"):
                    (output / "arm-owner-lock").write_text("reject", encoding="ascii")
                async with asyncio.timeout(55):
                    while True:
                        await expect(send).to_be_enabled(timeout=20000)
                        async with page.expect_response(lambda reply: reply.request.method == "POST"
                                                        and urlsplit(reply.url).path == message_path) as receipt:
                            await send.click()
                        response = await receipt.value
                        if response.ok:
                            break
                        error = await response.json()
                        assert response.status == 409 and error.get("detail", {}).get("code") == "storage_quota_busy", error
                        rejected += 1
                        await expect(composer).to_have_value(PROMPT)
                        await expect(chips).to_have_count(1)
                        assert await load_attachment(chips.locator("img")) == attachment_bytes
                        await asyncio.sleep(1.0)
                report["storage_busy_rejections_preserved_draft"] = rejected
                if data.get("owner_lock_rejection"):
                    assert rejected == 1, rejected
                rendered_answer = page.locator('.agent-panel [data-testid="chat-markdown"][data-authoritative="true"]').filter(has_text=data["nonce"])
                await expect(rendered_answer).to_be_visible(timeout=150000)
                await expect(chips).to_have_count(0)
                assert len(submissions) == 1 + rejected and all(item["content"] == PROMPT for item in submissions)
                submitted = submissions[-1]["images"]
                assert len(submitted) == 1 and submitted[0]["mime"] == "image/png"
                assert base64.b64decode(submitted[0]["data"]) == attachment_bytes
                detail_path = base + "/api/v1/conversations/" + str(data["conversation_id"])
                async with asyncio.timeout(45):
                    while True:
                        detail = await (await context.request.get(detail_path, headers=headers)).json()
                        messages = [event["payload"] for event in detail["events"] if event["type"] == "message"]
                        if any(message.get("role") == "assistant" and message.get("content", "").strip() == data["nonce"] for message in messages):
                            break
                        await asyncio.sleep(.2)
                user_message = next(message for message in messages if message.get("role") == "user")
                assert user_message["content"] == PROMPT and len(user_message["images"]) == 1
                stored = user_message["images"][0]
                raw = base + f"/api/v1/cases/{data['case_id']}/raw?" + urlencode({"path": stored["path"]})
                readback = await context.request.get(raw, headers=headers)
                assert readback.status == 200 and (await readback.body()) == attachment_bytes
                denied = await context.request.get(raw, headers={"Authorization": "Bearer " + data["outsider_token"]})
                assert denied.status == 404
                await page.reload(wait_until="domcontentloaded")
                await select_case(data["case_name"])
                await tab("Agent")
                history_image = page.get_by_test_id("chat-image")
                await expect(history_image).to_be_visible(timeout=20000)
                await page.wait_for_function("document.querySelector('[data-testid=chat-image]')?.naturalWidth > 0")
                await expect(rendered_answer).to_be_visible()
                await page.screenshot(path=output / "agent-vision-history.png")
                report["vision"] = {"exact_nonce": data["nonce"], "image_only_nonce": True,
                                    "authenticated_byte_readback": True, "other_account_status": denied.status}
                checks.append("real upstream extracted nonce present only in PNG; screenshot persists after reload and outsider is denied")

            if session_id:
                terminal_path = base + "/api/v1/terminal/sessions/" + session_id
                await tab("终端")
                await terminal.locator("[data-session-id=" + json.dumps(session_id) + "]").get_by_role(
                    "button", name=re.compile("关闭终端")
                ).click(timeout=20000)
                async with asyncio.timeout(65):
                    while True:
                        response = await context.request.get(terminal_path, headers=headers)
                        state = await response.json()
                        if state.get("phase") == "closed":
                            break
                        await asyncio.sleep(.2)
                assert state["cleanup_verified"] and state["stop"]["verified"], state
                denied = await context.request.get(terminal_path, headers={"Authorization": "Bearer " + data["outsider_token"]})
                assert denied.status == 404
                report.update(session=session_id, verified_stop=True, other_account_terminal_status=denied.status)
                session_id = None
            report.update(passed=True, browser=browser.version, requests_from_browser=len(submissions), pty_output_bytes=sum(map(len, frames)))
        except Exception as error:
            report.update(passed=False, stage=checks[-1] if checks else None, error=type(error).__name__, detail=str(error)[:2200])
            await page.screenshot(path=output / "agent-browser-failure.png")
            if session_id:
                await context.request.delete(base + "/api/v1/terminal/sessions/" + session_id, headers=headers, timeout=30000)
            raise
        finally:
            (output / "terminal-agent-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
            await browser.close()


def run_case(args, database_url, store):
    from testsupport import IsolatedServer

    configuration = None
    fixture = "from app.main import app\n"
    if args.source_profile_id:
        from live_llm_acceptance import FIXTURE, source
        configuration = asyncio.run(source(args.source_profile_id))
        fixture = FIXTURE + VISION_OUTBOUND_OBSERVER
        if args.owner_lock_rejection:
            from live_llm_agent_attachment_lock_acceptance import ADVISORY_LOCK
            fixture += ADVISORY_LOCK
    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    fixture += "\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n"

    if store:
        fixture = "import os\nos.environ.update(" + repr(dict(store.settings())) + ")\n" + fixture
    api = IsolatedServer("terminal-agent", app="browser_fixture:app", database_url=database_url)
    print("TERMINAL_AGENT_EVIDENCE " + str(api.root), flush=True)
    (api.root / "browser_fixture.py").write_text(fixture, encoding="utf8")
    nonce = str(10000000 + secrets.randbelow(90000000))
    (api.root / "expected-nonce.txt").write_text(nonce, encoding="ascii")
    with api:
        primary = api.create_case("Terminal Agent Transfer Source")
        workspace = api.call("GET", "/workspaces")[0]
        second = api.call("POST", f"/workspaces/{workspace['id']}/cases", {"name": "Terminal Agent Other Case"})
        visitor = api.call("POST", "/auth/register", {"email": "terminal-other@example.com",
                           "password": "isolated-test-234", "name": "Terminal other"})
        contents = (
            "import sys\n"
            "if len(sys.argv) == 2:\n"
            "    print('HIDDEN_SCROLLBACK_SENTINEL')\n"
            "    for index in range(1, 23): print(f'RECENT_TERMINAL_LINE_{index:02d}')\n"
            "else:\n"
            "    print('SCREENSHOT WINDOW')\n"
            f"print('\\x1b[38;2;255;255;255m\\x1b[48;2;{';'.join(map(str,COLOR))}m{SELECTED}\\x1b[0m')\n"
            "print('VISION CODE:', sys.argv[1])\n"
        )
        api.put("Ref/terminal-capture.py", contents)
        body = {"case_id": primary["id"], "title": "Terminal Agent Transfer", "permission_mode": "plan"}
        if configuration:
            profile = api.call("POST", "/provider-profiles", {"provider": "deepseek", "name": "Authorized isolated vision",
                "base_url": configuration["base_url"], "default_model": configuration["model"], "api_key": configuration["key"]})
            body.update(provider_profile_id=profile["id"], model=configuration["model"])
        conversation = api.call("POST", "/conversations", body)
        api.call("POST", "/conversations", {"case_id": second["id"], "title": "Other Case Draft"})
        data = {"root": str(api.root), "base": api.base, "token": api.token, "case_name": primary["name"],
                "case_id": primary["id"], "conversation_id": conversation["id"], "other_case": second["name"],
                "outsider_token": visitor["token"], "nonce": nonce, "channel": args.channel,
                "live_vision": bool(configuration), "owner_lock_rejection": args.owner_lock_rejection}
        subprocess.run([str(args.browser_python), "-B", "-X", "utf8", str(Path(__file__).resolve()), "--browser"],
                       input=json.dumps(data), text=True, timeout=360, check=True,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        if configuration:
            receipt = api.root / "upstream.jsonl"
            upstream = [json.loads(line) for line in receipt.read_text(encoding="utf8").splitlines()]
            calls = [event for event in upstream if event["kind"] == "http" and event["path"].endswith("/chat/completions")]
            assert len(calls) >= 1 and all(call["status"] == 200 for call in calls), calls
            assert any(event["kind"] == "stream_identity" for event in upstream), upstream
            inputs = [event for event in upstream if event["kind"] == "vision_input"]
            digest = hashlib.sha256((api.root / "terminal-attachment.png").read_bytes()).hexdigest()
            assert len(inputs) == len(calls) and not inputs[0]["nonce_in_text"]
            assert all(item["image_digests"] == [digest] and not item["nonce_in_tool_result"] for item in inputs), inputs
            if args.owner_lock_rejection:
                blocked, = [event for event in upstream if event["kind"] == "owner_lock_wait"]
                assert blocked["status"] == 409 and blocked["detail"]["code"] == "storage_quota_busy"
                assert blocked["seconds"] >= 1.4 and len(calls) == 1, blocked
                assert [event for event in upstream if event["kind"] == "competing_owner_lock"] == [
                    {"kind": "competing_owner_lock", "acquired": True}, {"kind": "competing_owner_lock", "released": True}]
    assert api.proc.poll() is not None
    print("TERMINAL_AGENT_OK " + str(api.root), flush=True)
    return api.root


@asynccontextmanager
async def disposable_database(enabled):
    if not enabled:
        yield None
        return
    import asyncpg
    from app.config import Settings
    from sqlalchemy.engine import make_url
    from workflow_migration_smoke import alembic

    configured = make_url(Settings(_env_file=SERVER / ".env").database_url)
    assert configured.drivername == "postgresql+asyncpg" and configured.host in {"127.0.0.1", "localhost", "::1"}
    name = "tmp_vf_termagent_" + uuid.uuid4().hex
    evidence = Path(tempfile.mkdtemp(prefix="terminal-agent-postgres-", dir=ROOT / "tmp"))
    admin = await asyncpg.connect(host=configured.host, port=configured.port or 5432, user=configured.username,
                                  password=configured.password, database="postgres", timeout=10)
    report = {"database": name, "created": False, "dropped": False}
    try:
        await admin.execute('CREATE DATABASE "' + name + '"')
        report["created"] = True
        url = configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic, url, evidence, "upgrade", "head")
        yield url
    finally:
        try:
            if report["created"]:
                assert await admin.fetchval("SELECT current_database()") == "postgres"
                await admin.execute('DROP DATABASE "' + name + '"')
                report["dropped"] = True
        finally:
            await admin.close()
            (evidence / "cleanup.json").write_text(json.dumps(report, indent=2), encoding="utf8")
            print("TERMINAL_AGENT_POSTGRES " + str(evidence), flush=True)


async def run(args):
    store = nullcontext(None)
    if args.s3:
        from s3testsupport import ObjectStore
        store = ObjectStore(executable=ROOT / "third_party/seaweed-4.52-runtime/weed", volume_max=6)
    async with disposable_database(args.postgres) as url:
        with store as active:
            if active:
                print("TERMINAL_AGENT_S3 " + str(active.root), flush=True)
            result = await asyncio.to_thread(run_case, args, url, active)
        if args.s3:
            assert active.proc is not None and active.proc.poll() is not None
            (result / "s3-cleanup.json").write_text(json.dumps({"root": str(active.root), "stopped": True}), encoding="utf8")


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        asyncio.run(browser_check(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--dist", type=Path, required=True)
        parser.add_argument("--browser-python", type=Path, required=True)
        parser.add_argument("--channel", default="msedge")
        parser.add_argument("--source-profile-id", type=int)
        parser.add_argument("--postgres", action="store_true")
        parser.add_argument("--s3", action="store_true")
        parser.add_argument("--owner-lock-rejection", action="store_true")
        args = parser.parse_args()
        if args.owner_lock_rejection and not (args.postgres and args.s3 and args.source_profile_id):
            parser.error("--owner-lock-rejection requires --postgres, --s3, and --source-profile-id")
        asyncio.run(run(args))
