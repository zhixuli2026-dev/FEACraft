"""F17：280/1200/6000 事件与 4/8/16MiB 分级历史边界。"""
import asyncio
import json
import time
import urllib.error
import urllib.request

from app.db import SessionLocal, engine
from app.models import ToolCall
from app.routers.agent import _append_event, _bounded_events, _load_events

B = "http://localhost:8000/api/v1"
EVENT_LOOP = asyncio.new_event_loop()


def run_async(awaitable):
    return EVENT_LOOP.run_until_complete(awaitable)


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def seed(cid, count, prefix="m", tool_records=False):
    async def run():
        async with SessionLocal() as db:
            for index in range(count):
                await _append_event(db, cid, "message", {"role": "user", "content": f"{prefix}{index}"})
                if tool_records:
                    db.add(ToolCall(id=f"{cid}:unprojected-{prefix}{index}", conversation_id=cid,
                                    tool="list_files", args="{}", status="executed"))
            await db.commit()
    run_async(run())


token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"history-{time.time_ns()}"}, token=token)
cid = call("POST", "/conversations", {"case_id": case["id"], "title": "history"}, token=token)["id"]
seed(cid, 350, tool_records=True)
page = call("GET", f"/conversations/{cid}", token=token)
assert len(page["events"]) == 280, len(page["events"])
assert page["events"][0]["payload"]["content"] == "m70", page["events"][0]
assert page["events"][-1]["payload"]["content"] == "m349", page["events"][-1]
assert page["event_window"]["has_more"] is True and page["event_window"]["next_offset"] == 280, page["event_window"]
assert page["tool_calls"] == [], "当前页之外/未引用的 ToolCall 不得绕过显示分页"
older = call("GET", f"/conversations/{cid}?offset=280&limit=280", token=token)
assert len(older["events"]) == 70 and older["events"][0]["payload"]["content"] == "m0", older["events"][:2]
assert older["events"][-1]["payload"]["content"] == "m69", older["events"][-1]
assert older["event_window"]["has_more"] is False, older["event_window"]
full = call("GET", f"/conversations/{cid}?limit=6000", token=token)
assert len(full["events"]) == 350 and full["event_window"]["has_more"] is False, full["event_window"]
assert full["tool_calls"] == [], "完整归档没有对应事件时也不应凭空生成工具卡"
print("1. 最新 280、向前 70、6000 完整日志分页且 ToolCall 不绕过窗口 [OK]")

seed(cid, 1000, prefix="x")
async def load_events(cid):
    async with SessionLocal() as db:
        return await _load_events(db, cid)


loaded = run_async(load_events(cid))
assert len(loaded) == 1200, len(loaded)
assert loaded[0]["payload"]["content"] == "m150", loaded[0]
assert loaded[-1]["payload"]["content"] == "x999", loaded[-1]
print("2. 模型上下文 1200 事件上限且保留最新 [OK]")

large = call("POST", "/conversations", {"case_id": case["id"], "title": "large"}, token=token)["id"]
async def seed_large():
    async with SessionLocal() as db:
        await _append_event(db, large, "message", {"role": "user", "content": "x" * (9 * 1024 * 1024)})
        await db.commit()
run_async(seed_large())
large_events = run_async(load_events(large))
assert len(large_events) == 1 and large_events[0]["payload"]["truncated"] is True, large_events[0]
assert len(json.dumps(large_events[0]["payload"], ensure_ascii=False).encode("utf-8")) <= 8 * 1024 * 1024
small = _bounded_events([{"payload": {"content": "x" * (1024 * 1024)}} for _ in range(5)], 280, 4 * 1024 * 1024)
assert len(small) == 3, len(small)
assert sum(len(json.dumps(e["payload"]).encode("utf-8")) for e in small) <= 4 * 1024 * 1024
archive = _bounded_events([{"payload": {"content": "x" * (1024 * 1024)}} for _ in range(5)], 6000, 16 * 1024 * 1024)
assert len(archive) == 5, len(archive)
assert sum(len(json.dumps(e["payload"]).encode("utf-8")) for e in archive) <= 16 * 1024 * 1024
print("3. 单事件 8MiB 截断、显示 4MiB、归档 16MiB [OK]")

other = call("POST", "/auth/register", {"email": f"history-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "history-other", "password": "other-pass-123"})
foreign = call("GET", f"/conversations/{cid}?offset=280&limit=280", token=other["token"])
assert foreign.get("_err") == 404, foreign
print("4. 跨用户分页 404 [OK]")
print("\n=== 分级历史边界冒烟通过 ===")
run_async(engine.dispose())
EVENT_LOOP.close()
