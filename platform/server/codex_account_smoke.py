"""F55：无凭据 Codex browser/device 登录状态机、账户/额度与环境缓存。"""
import asyncio
from pathlib import Path
import shutil
import sys
import tempfile

from app.services.codex_account import CodexDisplayCache, CodexLoginSession
from app.services.codex_stdio import start_codex_stdio
from app.services.codex_environment import create_isolated_codex_environment

FAKE = r'''
import json,sys
last_login=None
for line in sys.stdin:
    try: request=json.loads(line)
    except Exception: continue
    method=request.get("method"); rid=request.get("id"); params=request.get("params",{})
    if method=="initialize":
        result={"userAgent":"fixture/1"}
    elif method=="initialized":
        continue
    elif method=="account/login/start" and params.get("type")=="chatgpt":
        last_login="browser-1"
        result={"type":"chatgpt","loginId":last_login,"authUrl":"https://example.test/auth"}
    elif method=="account/login/start" and params.get("type")=="chatgptDeviceCode":
        last_login="device-1"
        result={"type":"chatgptDeviceCode","loginId":last_login,"verificationUrl":"https://example.test/device","userCode":"CODE123"}
    elif method=="account/login/cancel":
        print(json.dumps({"jsonrpc":"2.0","method":"account/login/completed","params":{"loginId":params["loginId"],"success":False,"error":"late completion"}}),flush=True)
        result={"status":"canceled"}
    elif method=="account/read":
        print(json.dumps({"jsonrpc":"2.0","method":"account/login/completed","params":{"loginId":last_login,"success":True}}),flush=True)
        result={"account":{"type":"chatgpt","email":"dev@example.test","planType":"plus"},"requiresOpenaiAuth":False}
    elif method=="account/rateLimits/read":
        result={"ordinaryUsageAllowed":True,"rateLimits":{"primary":{"usedPercent":25,"windowDurationMins":60,"resetsAt":123456}}}
    else:
        print(json.dumps({"jsonrpc":"2.0","id":rid,"error":{"message":"unknown"}}),flush=True)
        continue
    print(json.dumps({"jsonrpc":"2.0","id":rid,"result":result}),flush=True)
'''


async def main():
    root = Path(tempfile.mkdtemp(prefix="vibeflow-codex-account-"))
    try:
        fake = root / "fake_account.py"
        fake.write_text(FAKE, encoding="utf-8")
        client = await start_codex_stdio([sys.executable, "-u", str(fake)], create_isolated_codex_environment(root / 'home'))
        session = CodexLoginSession(client)
        try:
            browser = await session.start_browser()
            assert browser == {"loginId": "browser-1", "authUrl": "https://example.test/auth"}
            assert not session.handle_event({"method": "account/login/completed",
                "params": {"loginId": "device-old", "success": True}})
            account = await client.get_account(refresh_token=True)
            event = await client.next_event()
            assert session.handle_event(event) and session.status == "completed"
            assert account["account"]["planType"] == "plus"
            assert session.auth_url is None

            device = await session.start_device_code()
            assert device["userCode"] == "CODE123" and session.status == "waiting"
            cancelled = await session.cancel()
            assert cancelled["status"] == "canceled" and session.status == "cancelled"
            late = await client.next_event()
            assert not session.handle_event(late) and session.status == "cancelled"
            limits = await client.get_rate_limits()
            assert limits["ordinaryUsageAllowed"] is True
            assert limits["rateLimits"]["primary"]["usedPercent"] == 25
            print("1. browser/device/cancel、loginId 隔离、账户与额度协议 [OK]")
        finally:
            await client.close()

        cache = CodexDisplayCache()
        windows = cache.key("windows", "C:/codex.exe")
        wsl = cache.key("wsl", "/usr/bin/codex", "vibeflow-linux")
        cache.update_success(windows, account=account["account"], rate_limits=limits["rateLimits"])
        cache.update_success(wsl, account={"type": "chatgpt", "email": "other@example.test"})
        cache.update_error(windows, "models", "offline")
        cached = cache.read(windows)
        assert cached["account"]["planType"] == "plus" and cached["errors"]["models"] == "offline"
        assert cache.read(wsl)["account"]["email"] == "other@example.test"
        cache.update_success(windows, models=[{"id": "model-a"}])
        assert cache.read(windows)["errors"] == {}
        cache.clear(windows)
        assert cache.read(windows) is None and cache.read(wsl) is not None
        for index in range(25):
            cache.update_success(cache.key("windows", f"path-{index}"), program={"version": str(index)})
        assert len(cache._entries) == 20 and cache.read(cache.key("windows", "path-0")) is None
        try:
            cache.update_success(windows, access_token="must-not-cache")
            raise AssertionError("令牌字段不得进入展示缓存")
        except ValueError:
            pass
        print("2. 环境隔离、失败保留/成功清错、身份清理、20 项剪枝和禁令牌缓存 [OK]")
        print("\n=== Codex 登录状态机/账户额度/环境缓存冒烟通过 ===")
    finally:
        shutil.rmtree(root, ignore_errors=True)


asyncio.run(main())
