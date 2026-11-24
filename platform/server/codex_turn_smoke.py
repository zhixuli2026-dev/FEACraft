"""F13/F30/F50：Codex 高层 turn、审批、容量续跑、取消/超时和 Goal 等待继续。"""
import asyncio
from pathlib import Path
import shutil
import sys
import tempfile

from app.services.codex_capacity import CAPACITY_CONTINUE_PROMPT
from app.services.codex_stdio import start_codex_stdio
from app.services.codex_environment import create_isolated_codex_environment
from app.services.codex_turn import CodexApprovalRequired, run_codex_turn

FAKE = r'''
import json,sys
capacity_seen=0
active_turn=None
active_thread=None
def finish(status="completed",error=None):
    print(json.dumps({"method":"turn/completed","params":{"threadId":active_thread,"turn":{"id":active_turn,"status":status,"error":error}}}),flush=True)
for line in sys.stdin:
    try: request=json.loads(line)
    except Exception: continue
    method=request.get("method"); rid=request.get("id"); params=request.get("params",{})
    if method is None:
        result=request.get("result",{})
        if result.get("decision")=="accept":
            print(json.dumps({"jsonrpc":"2.0","id":101,"method":"item/tool/requestUserInput","params":{"threadId":active_thread,"turnId":active_turn,"questions":[{"id":"secret","isSecret":True,"question":"token"}]}}),flush=True)
        elif "answers" in result:
            finish()
        continue
    if method=="initialize":
        result={"userAgent":"fixture/1"}
    elif method=="initialized":
        continue
    elif method=="turn/start":
        active_thread=params["threadId"]
        text=params["input"][0]["text"]
        if text=="capacity":
            capacity_seen+=1
            if capacity_seen==1:
                active_turn="turn-1";finish("failed",{"message":"serverOverloaded"})
            else:
                active_turn="turn-2";finish()
        elif text=="quota":
            active_turn="turn-quota";finish("failed",{"message":"quota exhausted"})
        elif text.startswith("Continue the existing turn"):
            active_turn="turn-2";finish()
        elif text=="approvals":
            active_turn="turn-4"
            print(json.dumps({"jsonrpc":"2.0","id":100,"method":"item/commandExecution/requestApproval","params":{"threadId":active_thread,"turnId":active_turn,"command":"printf ok"}}),flush=True)
        elif text=="goal":
            active_turn="turn-5";finish()
        else:
            active_turn="turn-slow"
        result={"turn":{"id":active_turn,"status":"inProgress"}}
    elif method=="turn/interrupt":
        assert params=={"threadId":active_thread,"turnId":active_turn}
        finish("interrupted")
        result={}
    else:
        print(json.dumps({"jsonrpc":"2.0","id":rid,"error":{"message":"unknown"}}),flush=True)
        continue
    print(json.dumps({"jsonrpc":"2.0","id":rid,"result":result}),flush=True)
'''


async def main():
    root = Path(tempfile.mkdtemp(prefix="vibeflow-codex-turn-"))
    try:
        fake = root / "fake_turn.py"
        fake.write_text(FAKE, encoding="utf-8")
        client = await start_codex_stdio([sys.executable, "-u", str(fake)], create_isolated_codex_environment(root / 'home'))
        try:
            delays = []
            async def record_sleep(delay):
                delays.append(delay)
            result = await run_codex_turn(client, "t1", "capacity", "client-capacity",
                sleep=record_sleep, jitter=lambda _: 0)
            assert result == {"status": "completed", "threadId": "t1", "lastTurnId": "turn-2"}, result
            assert delays == [5.0], delays
            print("1. 容量失败保留线程并短继续，非原消息重发 [OK]")

            try:
                await run_codex_turn(client, "t1", "quota", "client-quota", sleep=record_sleep)
                raise AssertionError("额度错误不得续跑")
            except Exception as error:
                assert "quota exhausted" in str(error)
            assert delays == [5.0]
            print("2. 非容量错误直接失败 [OK]")

            seen = []
            async def handle(event):
                seen.append(event["method"])
                if event["method"] == "item/commandExecution/requestApproval":
                    await client.respond_to_approval(event["id"], "accept")
                elif event["method"] == "item/tool/requestUserInput":
                    await client.respond_to_user_input(event["id"], {"secret": ["dummy-secret"]})
            approved = await run_codex_turn(client, "t1", "approvals", "client-approval", on_event=handle)
            assert approved["lastTurnId"] == "turn-4"
            assert seen == ["item/commandExecution/requestApproval", "item/tool/requestUserInput",
                            "turn/completed"], seen
            try:
                await run_codex_turn(client, "t1", "approvals", "client-no-handler")
                raise AssertionError("无回调不得自动批准")
            except CodexApprovalRequired as error:
                assert error.event["method"] == "item/commandExecution/requestApproval"
            print("3. 审批/问答回调通过，无回调 fail closed [OK]")

            goal = await run_codex_turn(client, "t1", "goal", "client-goal", goal=True)
            assert goal == {"status": "waiting_continue", "threadId": "t1", "lastTurnId": "turn-5"}, goal
            print("4. Codex Goal 完成后等待继续，不叠加自动续轮 [OK]")

            cancel = asyncio.Event()
            task = asyncio.create_task(run_codex_turn(client, "t1", "slow", "client-slow", cancel=cancel))
            await asyncio.sleep(0.05)
            cancel.set()
            try:
                await task
                raise AssertionError("停止应中断 turn")
            except asyncio.CancelledError:
                pass
            assert client._events.empty() and not client._server_requests

            try:
                await run_codex_turn(client, "t1", "timeout", "client-timeout", turn_timeout_seconds=0.05)
                raise AssertionError("无完成事件应超时")
            except TimeoutError:
                pass
            assert client._events.empty() and not client._server_requests
            print("5. 停止/超时 interrupt 且不误重试 [OK]")
            print("\n=== Codex 高层 turn 执行器冒烟通过 ===")
        finally:
            await client.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


asyncio.run(main())
