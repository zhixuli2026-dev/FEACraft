"""终端多会话冒烟：新建→断开重挂（backlog 回放）→挂接续输入→列表→关闭

语义对齐桌面版终端多标签：WS 断开不杀 PTY，挂接补发 backlog，DELETE 显式关闭。
"""
import asyncio
import json
import os
import time

import aiohttp

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1").removesuffix("/api/v1")
WS = B.replace("http", "ws", 1)


def _token():
    import urllib.request
    req = urllib.request.Request(
        B + "/api/v1/auth/login", method="POST",
        data=json.dumps({"email": "dev@vibeflow.cn", "password": "devpass123"}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["token"]


def _register(email, password):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        B + "/api/v1/auth/register", method="POST",
        data=json.dumps({"email": email, "name": "terminal-security", "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req).read())["token"]
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
    req = urllib.request.Request(
        B + "/api/v1/auth/login", method="POST",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["token"]


async def main():
    owner_token = _token()
    other_token = _register(f"terminal-security-{time.time_ns()}@vibeflow.cn", "terminal-security-pass")
    Q = "token=" + owner_token
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    async with aiohttp.ClientSession() as s:
        # 1. 新建会话
        ws = await s.ws_connect(WS + "/ws/terminal?cols=80&rows=24&shell=cmd&" + Q)
        ready = json.loads((await ws.receive()).data)
        sid = ready["session"]
        assert ready["alive"], ready
        await ws.send_str(json.dumps({"type": "input", "data": "echo TERM_MARK_A\r"}))
        await asyncio.sleep(2)
        await ws.close()
        print("1. 新建会话", sid, "[OK]")

        # 2. 断开重挂：backlog 回放含标记
        ws2 = await s.ws_connect(f"{WS}/ws/terminal?session={sid}&" + Q)
        ready2 = json.loads((await ws2.receive()).data)
        assert ready2["session"] == sid
        got = b""
        t0 = time.time()
        while time.time() - t0 < 5:
            msg = await ws2.receive()
            if msg.type == aiohttp.WSMsgType.BINARY:
                got += msg.data
                if b"TERM_MARK_A" in got:
                    break
        assert b"TERM_MARK_A" in got, got[-200:]
        print("2. 重挂 backlog 回放 [OK]")

        # 3. 挂接后继续输入仍工作
        await ws2.send_str(json.dumps({"type": "input", "data": "echo TERM_MARK_B\r"}))
        got2 = b""
        t0 = time.time()
        while time.time() - t0 < 5:
            msg = await ws2.receive()
            if msg.type == aiohttp.WSMsgType.BINARY:
                got2 += msg.data
                if b"TERM_MARK_B" in got2:
                    break
        assert b"TERM_MARK_B" in got2, got2[-200:]
        print("3. 挂接后继续输入 [OK]")
        await ws2.close()

        # 4. 未认证与其他用户均不能枚举此会话
        async with s.get(B + "/api/v1/terminal/sessions") as r:
            assert r.status in (401, 403), f"未认证列表应拒绝，实际 {r.status}"
        async with s.get(B + "/api/v1/terminal/sessions",
                         headers=other_headers) as r:
            other_list = (await r.json())["sessions"]
        assert not any(x["id"] == sid for x in other_list), other_list
        async with s.get(B + "/api/v1/terminal/sessions",
                         headers=owner_headers) as r:
            lst = (await r.json())["sessions"]
        assert any(x["id"] == sid and x["alive"] for x in lst), lst
        print("4. 会话列表认证与隔离 [OK]", len(lst), "个所有者会话")

        # 5. 其他用户不能挂接或关闭；所有者可以关闭
        foreign = await s.ws_connect(f"{WS}/ws/terminal?session={sid}&token={other_token}")
        msg = await foreign.receive()
        assert msg.type == aiohttp.WSMsgType.CLOSE and (
            msg.data == 4403 or foreign.close_code == 4403), (msg, foreign.close_code)
        await foreign.close()
        async with s.delete(f"{B}/api/v1/terminal/sessions/{sid}",
                            headers=other_headers) as r:
            assert r.status == 404, await r.text()
        async with s.get(B + "/api/v1/terminal/sessions",
                         headers=owner_headers) as r:
            lst = (await r.json())["sessions"]
        assert any(x["id"] == sid for x in lst), lst
        async with s.delete(f"{B}/api/v1/terminal/sessions/{sid}") as r:
            assert r.status in (401, 403), f"未认证删除应拒绝，实际 {r.status}"
        async with s.delete(f"{B}/api/v1/terminal/sessions/{sid}",
                            headers=owner_headers) as r:
            assert (await r.json())["ok"]
        async with s.get(B + "/api/v1/terminal/sessions",
                         headers=owner_headers) as r:
            lst2 = (await r.json())["sessions"]
        assert not any(x["id"] == sid for x in lst2)
        print("5. 跨用户挂接/删除拒绝，所有者 DELETE 关闭 [OK]")


if __name__ == "__main__":
    asyncio.run(main())
    print("\n=== 终端多会话冒烟通过 ===")
