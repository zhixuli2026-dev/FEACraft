"""Browser quick launch, real nonprivileged WSL Codex, real Responses service."""
import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import urlsplit

PROMPT = "Use a shell tool to run cat Ref/wsl-codex-proof.txt. Reply with exactly its contents. Do not modify files or use any other tool."
ENV_KEYS = ("PYTHONUTF8", "PYTHONIOENCODING", "TERM")
PROFILE = dict(zip(ENV_KEYS, ("1", "utf-8", "xterm-256color")))
WSL_PYTHON = ["wsl.exe", "-d", "vibeflow-linux", "--user", "nobody", "--exec", "python3", "-I", "-S"]


@contextmanager
def isolated_cli_environment(values):
    original = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def isolated_wsl_home(options):
    create = """
from pathlib import Path
import sys, tempfile
root = Path(tempfile.mkdtemp(prefix='vibeflow-g05-home-',dir='/tmp'))
(root/'config.toml').write_text(sys.stdin.read(),encoding='utf8')
(root/'tmp').mkdir(mode=0o700)
print(root)
"""
    result = subprocess.run(WSL_PYTHON + ["-c", create], input=options, capture_output=True, text=True,
                            encoding="utf8", check=True, timeout=20)
    native = result.stdout.strip()
    assert re.fullmatch(r"/tmp/vibeflow-g05-home-[A-Za-z0-9_]+", native)
    try:
        yield native
    finally:
        remove = """
from pathlib import Path
import os, shutil, sys
root = Path(sys.argv[1]); directory = root.lstat()
assert root.resolve() == root and root.parent == Path('/tmp') and root.name.startswith('vibeflow-g05-home-')
assert directory.st_uid == os.getuid() and directory.st_mode & 0o777 == 0o700 and (root/'config.toml').is_file()
shutil.rmtree(root)
assert not root.exists()
"""
        subprocess.run(WSL_PYTHON + ["-c", remove, native], check=True, timeout=20)


def read_native_codex_environment(home):
    # Restrict the observation to this fixture's synthetic CLI home and select
    # only the three public variables. Never return other environment values.
    probe = """
import json, os, pathlib, sys
home = os.fsencode(sys.argv[1])
public = ('PYTHONUTF8', 'PYTHONIOENCODING', 'TERM')
results = []
for process in pathlib.Path('/proc').glob('[0-9]*'):
    try:
        if process.stat().st_uid != os.getuid() or pathlib.Path(os.readlink(process/'exe')).name != 'codex':
            continue
        values = dict(entry.split(b'=', 1) for entry in (process/'environ').read_bytes().split(b'\\0') if b'=' in entry)
        if values.get(b'CODEX_HOME') == home:
            results.append({'uid': os.getuid(), 'environment': {name: os.fsdecode(values.get(name.encode(),b'')) for name in public}})
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
print(json.dumps(results))
"""
    result = subprocess.run(WSL_PYTHON + ["-c", probe, home],
                            capture_output=True, text=True, encoding="utf8", check=True, timeout=20)
    return json.loads(result.stdout)


def read_native_turn_proof(home, marker):
    reader = """
import json, pathlib, sys
home, marker = pathlib.Path(sys.argv[1]), sys.argv[2]
tool_seen = exact_reply = completed_turn = False
files = list((home/'sessions').rglob('*.jsonl'))
for path in files:
    for line in path.read_text(encoding='utf8').splitlines():
        value = json.loads(line)
        if value.get('type') == 'event_msg' and value.get('payload', {}).get('type') in ('task_complete', 'turn_complete'):
            completed_turn = True
        if value.get('type') != 'response_item': continue
        item = value['payload']
        if item.get('type') == 'function_call' and 'cat Ref/wsl-codex-proof.txt' in str(item.get('arguments', '')):
            tool_seen = True
        if item.get('role') == 'assistant':
            exact_reply |= any(part.get('text','').strip() == marker for part in item.get('content', []) if isinstance(part,dict))
print(json.dumps({'native_file_read_tool':tool_seen,'exact_final_reply':exact_reply,
                  'completed_turn_event':completed_turn,'sessions':len(files)}))
"""
    result = subprocess.run(WSL_PYTHON + ["-c", reader, home, marker], capture_output=True, text=True,
                            encoding="utf8", check=True, timeout=20)
    return json.loads(result.stdout)


async def browser(data):
    from playwright.async_api import async_playwright, expect

    base, root = data["base"], Path(data["root"])
    headers = {"Authorization": "Bearer " + data["token"]}
    inputs, sessions, native = [], [], []
    result = {"passed": False, "checks": [], "mock_llm_calls": 0, "inference_entry": "interactive"}
    async with async_playwright() as driver:
        chromium = await driver.chromium.launch(channel=data["channel"])
        context = await chromium.new_context(viewport={"width": 1536, "height": 1100})
        page = await context.new_page()
        await page.add_init_script("if(location.origin === " + json.dumps(base) + ") localStorage.setItem('vf_token', " + json.dumps(data["token"]) + ")")
        cdp = await context.new_cdp_session(page)

        def observe(socket):
            url = urlsplit(socket.url)
            if url.path != "/ws/terminal":
                return

            def sent(frame):
                if isinstance(frame, str):
                    value = json.loads(frame)
                    if value.get("type") == "input":
                        inputs.append(value["data"])

            socket.on("framesent", sent)

        page.on("websocket", observe)
        terminal = page.get_by_test_id("terminal-panel")
        rows = terminal.locator(".xterm-rows")
        entry = terminal.locator(".xterm-helper-textarea")
        tabs = terminal.locator(".terminal-session-tab")
        picker = terminal.get_by_test_id("cli-profile")
        command_button = terminal.get_by_role("button", name="codex ▸", exact=True)
        new_button = terminal.get_by_role("button", name="新建终端", exact=True)
        cmd_kind = terminal.get_by_role("button", name="Windows cmd", exact=True)
        wsl_kind = terminal.get_by_role("button", name="WSL", exact=True)

        async def text_input(text):
            await entry.focus()
            await cdp.send("Input.insertText", {"text": text})
            await page.keyboard.press("Enter")

        async def running(shell, previous=None):
            async with asyncio.timeout(85):
                matches = tabs.filter(has_text=re.compile(r"^" + shell + r"\s*·"))
                while True:
                    for row in await matches.all():
                        sid = await row.get_attribute("data-session-id")
                        if sid and sid != previous and "运行中" in (await row.inner_text()):
                            if sid not in sessions:
                                sessions.append(sid)
                            return sid, row
                    await asyncio.sleep(.15)

        async def close(identifier, row):
            await row.get_by_role("button", name=re.compile("^关闭终端")).click()
            async with asyncio.timeout(60):
                while True:
                    response = await context.request.get(base + "/api/v1/terminal/sessions/" + identifier, headers=headers)
                    state = await response.json()
                    if state.get("phase") == "closed":
                        assert state["cleanup_verified"] and state["stop"]["verified"], state
                        sessions.remove(identifier)
                        return
                    await asyncio.sleep(.2)

        async def environment(label):
            await text_input("printf '\\n" + label + "=%s|%s|%s\\n' \"${PYTHONUTF8-unset}\" \"${PYTHONIOENCODING-unset}\" \"${TERM-unset}\"")
            expression = re.compile(label + r"=(?:unset|1)\|(?:unset|utf-8)\|[A-Za-z0-9_-]+")
            await expect(rows).to_contain_text(expression, timeout=20000)
            return expression.search(await rows.inner_text()).group(0).split("=", 1)[1]

        try:
            await page.goto(base, wait_until="domcontentloaded")
            await page.locator("[data-case-name=" + json.dumps(data["case"]) + "]").click(timeout=30000)
            tab = page.locator(".dv-tab").filter(has_text=re.compile("^终端$"))
            await tab.click()
            box = await tab.bounding_box()
            assert box
            await page.mouse.move(768, box["y"])
            await page.mouse.down()
            await page.mouse.move(768, box["y"] - 300, steps=18)
            await page.mouse.up()

            await wsl_kind.click()
            await new_button.click()
            original_id, original = await running("wsl")
            await expect(rows).to_contain_text("nobody@", timeout=25000)
            baseline = await environment("VF_WSL_BASELINE")
            await cmd_kind.click()
            await picker.select_option("codex-wsl")
            await expect(command_button).to_be_enabled(timeout=60000)
            await command_button.click()
            codex_id, codex = await running("wsl", original_id)
            await expect(cmd_kind).to_have_attribute("aria-pressed", "true")

            invocation = "env PYTHONUTF8='1' PYTHONIOENCODING='utf-8' TERM='xterm-256color' codex\r"
            async with asyncio.timeout(35):
                while not native:
                    native = await asyncio.to_thread(read_native_codex_environment, data["native_home"])
                    if not native:
                        await asyncio.sleep(.4)
            assert len([value for value in inputs if value == invocation]) == 1, [len(value) for value in inputs]
            assert all(info == {"uid": 65534, "environment": PROFILE} for info in native), native
            result["checks"].append("browser profile launched actual nonprivileged Codex with only its child overrides")
            result["native"] = native

            landing = re.compile(r"OpenAI Codex|trust the contents|trust this|trust this directory|codex uses", re.I)
            await expect(rows).to_contain_text(landing, timeout=30000)
            text = await rows.inner_text()
            if re.search(r"trust (?:this|the contents|files in)", text, re.I) and not re.search(r"deepseek-flash", text, re.I):
                await page.keyboard.press("Enter")
            await expect(rows).to_contain_text(re.compile(r"deepseek-flash\s+low", re.I), timeout=45000)
            await expect(rows).not_to_contain_text(re.compile(r"loading|waiting for startup", re.I), timeout=30000)
            await entry.focus()
            await cdp.send("Input.insertText", {"text": PROMPT})
            await expect(rows).to_contain_text("Use a shell tool to run cat Ref/wsl-codex-proof.txt", timeout=20000)
            await page.keyboard.press("Enter")
            await expect(rows).to_contain_text(data["marker"], timeout=165000)
            await page.screenshot(path=root / "codex-wsl-terminal.png")
            result["checks"].append("genuine Codex replied with the exact random case file contents")

            await text_input("/quit")
            await expect(rows).to_contain_text("nobody@", timeout=25000)
            after = await environment("VF_WSL_AFTER_CODEX")
            assert baseline == after, (baseline, after)
            await original.get_by_role("button").first.click()
            await expect(rows).to_contain_text("VF_WSL_BASELINE", timeout=20000)
            other = await environment("VF_WSL_OTHER_SESSION")
            assert other == baseline, (baseline, other)
            await new_button.click()
            cmd_id, cmd = await running("cmd")
            await expect(rows).to_contain_text(re.compile(r"[A-Za-z]:\\[^>]+>"), timeout=20000)
            await text_input("echo VF_WIN_UNCHANGED")
            await expect(rows).to_contain_text("VF_WIN_UNCHANGED", timeout=12000)
            result["checks"].append("previous cmd selection and original WSL environment survive the compatibility launch")

            for identifier, row in [(cmd_id, cmd), (codex_id, codex), (original_id, original)]:
                await close(identifier, row)
            result["checks"].append("three actual terminal session exits independently verified")
            result.update(passed=True, browser=chromium.version)
        except Exception as error:
            result.update(error=type(error).__name__, detail=str(error)[:2200])
            await page.screenshot(path=root / "codex-wsl-failure.png")
            for identifier in list(sessions):
                await context.request.delete(base + "/api/v1/terminal/sessions/" + identifier, headers=headers, timeout=15000)
            raise RuntimeError(f"Codex WSL browser proof failed: {type(error).__name__}. See {root / 'codex-wsl-browser.json'}") from None
        finally:
            (root / "codex-wsl-browser.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            await chromium.close()


def run_case(args, config, db_url, store):
    from app.services.codex_credentials import SCHEMA, TOKEN_ENV, config as render_config, toml
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    api = IsolatedServer("codex-wsl-profile", app="codex_wsl_fixture:app", database_url=db_url)
    print("CODEX_WSL_PROFILE_EVIDENCE " + str(api.root), flush=True)
    body = "import os\n" + ("os.environ.update(" + repr(dict(store.settings())) + ")\n" if store else "")
    body += "from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
    body += "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n"
    (api.root / "codex_wsl_fixture.py").write_text(body, encoding="utf8")
    marker = "WSL_PROFILE_" + secrets.token_hex(12)
    provider_base = "https://api.deepseek.com"
    binding = {"schema": SCHEMA, "profile_id": 1, "owner_id": 1, "provider": "deepseek", "base_url": provider_base,
               "default_model": config["model"], "wire_api": "responses", "revision": "a" * 64}
    options = (
        "model = " + toml(config["model"]) + "\n"
        'approval_policy = "never"\nsandbox_mode = "danger-full-access"\nweb_search = "disabled"\n'
        'model_reasoning_effort = "low"\ncheck_for_update_on_startup = false\n' + render_config(binding)
    )
    with isolated_wsl_home(options) as native:
        exported = {"CODEX_HOME": native, "TMPDIR": native + "/tmp", TOKEN_ENV: config["key"],
                    "WSLENV": f"CODEX_HOME/u:TMPDIR/u:{TOKEN_ENV}/u:NO_PROXY/u", "NO_PROXY": "api.deepseek.com"}
        with isolated_cli_environment(exported):
            with api:
                case = api.create_case("Codex WSL compatibility proof")
                assert api.call("GET", "/runtime/cli-profiles")["codex-wsl"] == {"shell": "wsl", "env": PROFILE}
                api.put("Ref/wsl-codex-proof.txt", marker)
                payload = {"base": api.base, "root": str(api.root), "token": api.token, "case": case["name"],
                           "native_home": native, "marker": marker, "channel": args.channel}
                browser_env = {name: value for name, value in os.environ.items() if name not in exported}
                subprocess.run([str(args.browser_python), "-B", "-X", "utf8", str(Path(__file__).resolve()), "--browser"],
                               input=json.dumps(payload), text=True, timeout=390, check=True, env=browser_env)
                assert api.call("GET", api.prefix + "/file?path=Ref/wsl-codex-proof.txt")["content"] == marker
        assert api.proc.poll() is not None
        proof = read_native_turn_proof(native, marker)
        (api.root / "codex-wsl-native-turn.json").write_text(json.dumps(proof, indent=2), encoding="utf8")
        assert proof["native_file_read_tool"] and proof["exact_final_reply"] and proof["completed_turn_event"], proof

    report = {"passed": True, "provider": "deepseek", "provider_base": provider_base, "model": config["model"],
              "completed_real_model_turns": 1, "request_count_observed": False,
              "mock_llm_calls": 0, "case_proof_sha256": hashlib.sha256(marker.encode()).hexdigest(),
              **proof, "api_exited": True, "isolated_wsl_home_removed": True,
              "inference_entry": "interactive"}
    (api.root / "codex-wsl-result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    print("CODEX_WSL_PROFILE_OK " + str(api.root), flush=True)


async def main(args):
    from contextlib import nullcontext
    from live_llm_acceptance import source
    from live_llm_terminal_agent_acceptance import disposable_database

    configuration = await source(args.source_profile_id)
    store = nullcontext()
    if args.s3:
        from s3testsupport import ObjectStore
        root = Path(__file__).resolve().parents[2]
        store = ObjectStore(executable=root / "third_party/seaweed-4.52-runtime/weed", volume_max=6)
    async with disposable_database(args.postgres) as database:
        with store as active:
            await asyncio.to_thread(run_case, args, configuration, database, active)
    if args.s3:
        assert active.proc is not None and active.proc.poll() is not None


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--dist", required=True, type=Path)
        parser.add_argument("--browser-python", required=True, type=Path)
        parser.add_argument("--source-profile-id", required=True, type=int)
        parser.add_argument("--channel", default="msedge")
        parser.add_argument("--postgres", action="store_true")
        parser.add_argument("--s3", action="store_true")
        asyncio.run(main(parser.parse_args()))
