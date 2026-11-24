"""双用户归属安全回归：终端会话与 LLM provider profile。

需要 :8000 后端和 :8990 mock LLM。所有外部 LLM 调用均指向 mock。
"""
import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import aiohttp

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=30) as r:
            raw = r.read()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"_status": r.status, "raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw).get("detail")
        except (json.JSONDecodeError, AttributeError):
            detail = raw.decode("utf-8", "replace")
        return {"_err": e.code, "detail": detail}


def expect_error(result, *codes):
    assert result.get("_err") in codes, result


def register_or_login(email, password):
    r = call("POST", "/auth/register",
             {"email": email, "name": "security-smoke", "password": password})
    if "token" in r:
        return r["token"]
    assert r.get("_err") == 409, r
    return call("POST", "/auth/login", {"email": email, "password": password})["token"]


def first_case(token):
    workspaces = call("GET", "/workspaces", token=token)
    ws_id = workspaces[0]["id"]
    cases = call("GET", f"/workspaces/{ws_id}/cases", token=token)
    for case in cases:
        if case["name"] == "cavity-demo":
            return case["id"]
    return call("POST", f"/workspaces/{ws_id}/cases",
                {"name": f"security-smoke-{time.time_ns()}"}, token=token)["id"]


def new_mock_profile(token, name):
    return call("POST", "/provider-profiles", {
        "provider": "custom", "name": name, "base_url": "http://127.0.0.1:8990",
        "default_model": "mock-model", "api_key": "mock-key"}, token=token)


def sse_collect(path, body, token):
    req = urllib.request.Request(B + path, method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(req, timeout=30) as resp:
        for line in resp:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data:"):
                events.append(json.loads(text[5:].strip()))
    return events


async def terminal_ownership(owner, other):
    sid = None
    async with aiohttp.ClientSession() as session:
        try:
            ws = await session.ws_connect(
                "ws://127.0.0.1:8000/ws/terminal?cols=80&rows=24&shell=cmd&token=" + owner)
            ready = json.loads((await ws.receive()).data)
            sid = ready["session"]
            await ws.close()

            expect_error(call("GET", "/terminal/sessions"), 401, 403)
            other_list = call("GET", "/terminal/sessions", token=other)["sessions"]
            assert not any(s["id"] == sid for s in other_list), other_list
            owner_list = call("GET", "/terminal/sessions", token=owner)["sessions"]
            assert any(s["id"] == sid for s in owner_list), owner_list
            print("1. 终端 REST 认证与列表隔离 [OK]")

            foreign = await session.ws_connect(
                f"ws://127.0.0.1:8000/ws/terminal?session={sid}&token={other}")
            msg = await foreign.receive()
            assert msg.type == aiohttp.WSMsgType.CLOSE and (
                msg.data == 4403 or foreign.close_code == 4403), (msg, foreign.close_code)
            await foreign.close()
            expect_error(call("DELETE", f"/terminal/sessions/{sid}", token=other), 404)
            owner_list = call("GET", "/terminal/sessions", token=owner)["sessions"]
            assert any(s["id"] == sid for s in owner_list), owner_list
            print("2. 终端跨用户挂接/删除拒绝 [OK]")

            expect_error(call("DELETE", f"/terminal/sessions/{sid}"), 401, 403)
            assert call("DELETE", f"/terminal/sessions/{sid}", token=owner).get("ok")
            sid = None
            print("3. 终端未认证删除拒绝、所有者关闭 [OK]")
        finally:
            if sid is not None:
                call("DELETE", f"/terminal/sessions/{sid}", token=owner)


async def make_legacy_reference(cid, profile_id, tool_id, path):
    from app.db import SessionLocal
    from app.models import Conversation, ToolCall

    async with SessionLocal() as db:
        conv = await db.get(Conversation, cid)
        conv.provider_profile_id = profile_id
        db.add(ToolCall(id=tool_id, conversation_id=cid, tool="propose_file_update",
                        args=json.dumps({"path": path, "content": "SHOULD_NOT_WRITE",
                                         "reason": "legacy profile ownership probe"}),
                        status="pending"))
        await db.commit()


def profile_ownership(owner, other, case_id):
    owner_profile = new_mock_profile(owner, f"security-owner-{time.time_ns()}")
    other_profile = new_mock_profile(other, f"security-other-{time.time_ns()}")
    other_pid = other_profile["id"]

    result = call("POST", "/conversations", {
        "case_id": case_id, "title": "cross-profile-create",
        "provider_profile_id": other_pid, "model": "mock-model"}, token=owner)
    expect_error(result, 404)
    print("4. 会话创建拒绝他人 profile [OK]")

    cid = call("POST", "/conversations", {
        "case_id": case_id, "title": f"legacy-profile-{time.time_ns()}",
        "provider_profile_id": owner_profile["id"], "model": "mock-model"}, token=owner)["id"]
    for i in range(3):
        events = sse_collect(f"/conversations/{cid}/messages", {
            "content": f"第{i + 1}轮", "provider_profile_id": owner_profile["id"],
            "model": "mock-model"}, owner)
        assert any(e["type"] == "run.completed" for e in events), events

    tool_id = f"security:{time.time_ns()}"
    path = f"D-post/security-{time.time_ns()}.md"
    asyncio.run(make_legacy_reference(cid, other_pid, tool_id, path))
    try:
        expect_error(call("POST", f"/conversations/{cid}/compress",
                          {"keep_last": 2}, token=owner), 404)
        expect_error(call("POST", f"/conversations/{cid}/fork", {}, token=owner), 404)
        print("5. 历史异常 profile 的压缩/分叉拒绝 [OK]")

        expect_error(call("POST", f"/conversations/{cid}/tool-calls/{tool_id}:approve",
                          {}, token=owner), 404)
        encoded = urllib.parse.quote(path, safe="")
        expect_error(call("GET", f"/cases/{case_id}/file?path={encoded}", token=owner), 404)
        expect_error(call("POST", f"/conversations/{cid}/messages", {
            "content": "cross profile", "provider_profile_id": other_pid,
            "model": "mock-model"}, token=owner), 404)
        print("6. 审批先拒越权且无写入、消息 profile 隔离 [OK]")
    finally:
        call("DELETE", f"/cases/{case_id}/file?path={urllib.parse.quote(path, safe='')}",
             token=owner)


def main():
    owner = register_or_login(f"security-owner-{time.time_ns()}@vibeflow.cn", "security-owner-pass-123")
    other = register_or_login(f"security-{time.time_ns()}@vibeflow.cn", "security-pass-123")
    case_id = first_case(owner)
    asyncio.run(terminal_ownership(owner, other))
    profile_ownership(owner, other, case_id)
    print("\n=== 双用户归属安全冒烟通过 ===")


if __name__ == "__main__":
    main()
