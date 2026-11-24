"""F56：Codex Windows 沙盒 readiness、显式 setup 与完成事件隔离。"""
import asyncio
from pathlib import Path
import shutil
import sys
import tempfile

from app.services.codex_sandbox import CodexSandboxSession
from app.services.codex_stdio import start_codex_stdio
from app.services.codex_environment import create_isolated_codex_environment

FAKE = r'''
import json,sys
configured=False
for line in sys.stdin:
    try: request=json.loads(line)
    except Exception: continue
    method=request.get("method"); rid=request.get("id"); params=request.get("params",{})
    if method=="initialize":
        result={"userAgent":"fixture/1"}
    elif method=="initialized":
        continue
    elif method=="windowsSandbox/readiness":
        result={"status":"ready" if configured else "notConfigured"}
    elif method=="windowsSandbox/setupStart":
        mode=params["mode"]; success=(mode=="elevated")
        print(json.dumps({"jsonrpc":"2.0","method":"windowsSandbox/setupCompleted","params":{"mode":mode,"success":success,"error":None if success else "denied"}}),flush=True)
        configured=configured or success
        result={"started":True}
    else:
        print(json.dumps({"jsonrpc":"2.0","id":rid,"error":{"message":"unknown"}}),flush=True)
        continue
    print(json.dumps({"jsonrpc":"2.0","id":rid,"result":result}),flush=True)
'''


async def main():
    root = Path(tempfile.mkdtemp(prefix="vibeflow-codex-sandbox-"))
    try:
        fake = root / "fake_sandbox.py"
        fake.write_text(FAKE, encoding="utf-8")
        client = await start_codex_stdio([sys.executable, "-u", str(fake)], create_isolated_codex_environment(root / 'home'))
        session = CodexSandboxSession(client)
        try:
            assert (await session.readiness())["status"] == "notConfigured"
            try:
                await session.setup("elevated", confirmed=False)
                raise AssertionError("未确认不得初始化")
            except ValueError:
                pass
            try:
                await session.setup("root", confirmed=True)
                raise AssertionError("非法模式不得初始化")
            except ValueError:
                pass
            started = await session.setup("elevated", confirmed=True)
            assert started == {"started": True} and session.status == "running"
            assert not session.handle_event({"method": "windowsSandbox/setupCompleted",
                "params": {"mode": "unelevated", "success": True}})
            completed = await client.next_event()
            assert session.handle_event(completed) and session.status == "ready"
            assert (await session.readiness())["status"] == "ready"

            await session.setup("unelevated", confirmed=True)
            failed = await client.next_event()
            assert session.handle_event(failed) and session.status == "failed"
            assert session.error == "denied"
            print("Codex Windows readiness、显式确认、模式匹配、成功/失败与迟到事件隔离: OK")
        finally:
            await client.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


asyncio.run(main())
