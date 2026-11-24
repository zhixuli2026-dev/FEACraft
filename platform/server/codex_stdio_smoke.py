"""F53：真实子进程 JSON-RPC/stdio、线程、审批响应、中断与流式事件。"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile

from app.services.codex_stdio import start_codex_stdio
from app.services.codex_environment import create_isolated_codex_environment

FAKE = r'''
import json,sys
approval_id=9000
initialized=False
for line in sys.stdin:
    try: request=json.loads(line)
    except Exception: continue
    method=request.get("method"); request_id=request.get("id"); params=request.get("params",{})
    if method is None and isinstance(request_id,int) and request_id>=9001:
        response=request.get("result",{})
        if "answers" in response:
            answers=response["answers"]
            print(json.dumps({"jsonrpc":"2.0","method":"userInput.resolved","params":{"requestId":request_id,"questionIds":sorted(answers),"answerCount":sum(len(item["answers"]) for item in answers.values())}}),flush=True)
        else:
            decision=response.get("decision")
            print(json.dumps({"jsonrpc":"2.0","method":"approval.resolved","params":{"requestId":request_id,"decision":decision}}),flush=True)
        continue
    if method=="initialize":
        initialized=True
        result={"userAgent":"fixture/1","codexHome":"isolated","platformFamily":"windows","platformOs":"windows"}
    elif method=="initialized":
        continue
    elif not initialized:
        raise RuntimeError("missing initialization")
    elif method=="thread/start":
        print(json.dumps({"jsonrpc":"2.0","method":"thread.started","params":{"threadId":"t1"}}),flush=True)
        print(json.dumps({"jsonrpc":"2.0","id":999,"result":{"threadId":"wrong"}}),flush=True)
        result={"thread":{"id":"t1"}}
    elif method=="thread/resume":
        assert "lastTurnId" not in params
        result={"thread":{"id":params["threadId"]},"excludeTurns":params["excludeTurns"]}
    elif method=="thread/fork":
        result={"thread":{"id":"t2"},"lastTurnId":params.get("lastTurnId","turn-2"),"excludeTurns":params.get("excludeTurns",False)}
    elif method=="turn/start":
        approval_id+=1
        user_text=(params.get("input") or [{}])[0].get("text")
        if user_text=="needs-input":
            print(json.dumps({"jsonrpc":"2.0","id":approval_id,"method":"item/tool/requestUserInput","params":{"threadId":"t2","turnId":"turn-3","itemId":"item-input","isBlocking":True,"questions":[{"id":"q-choice","header":"Choice","question":"Pick","options":[{"label":"Option A","description":"A"}]},{"id":"q-free","header":"Free","question":"Text","isOther":True},{"id":"q-secret","header":"Secret","question":"Token","isSecret":True}]}}),flush=True)
        else:
            print(json.dumps({"jsonrpc":"2.0","id":approval_id,"method":"item/commandExecution/requestApproval","params":{"threadId":"t1","command":"printf test","clientUserMessageId":params.get("clientUserMessageId"),"input":params.get("input")}}),flush=True)
        result={"turn":{"id":"turn-3","status":"inProgress"}}
    elif method=="turn/interrupt":
        assert params=={"threadId":"t2","turnId":"turn-3"}
        result={}
    else:
        print(json.dumps({"jsonrpc":"2.0","id":request_id,"error":{"message":"unknown method"}}),flush=True)
        continue
    print(json.dumps({"jsonrpc":"2.0","id":request_id,"result":result}),flush=True)
'''


async def main():
    root = Path(tempfile.mkdtemp(prefix="vibeflow-codex-stdio-"))
    try:
        fake = root / "fake_app.py"
        fake.write_text(FAKE, encoding="utf-8")
        client = await start_codex_stdio([sys.executable, "-u", str(fake)], create_isolated_codex_environment(root / 'home'))
        try:
            created = await client.new_thread()
            assert created == {"thread": {"id": "t1"}}, created
            event = await client.next_event()
            assert event["method"] == "thread.started" and event["params"]["threadId"] == "t1", event
            resumed = await client.resume_thread("t1", exclude_turns=True)
            assert resumed == {"thread": {"id": "t1"}, "excludeTurns": True}, resumed
            forked = await client.fork_thread("t1")
            assert forked == {"thread": {"id": "t2"}, "lastTurnId": "turn-2", "excludeTurns": False}, forked
            bounded_fork = await client.fork_thread("t1", "turn-1", True)
            assert bounded_fork == {"thread": {"id": "t2"}, "lastTurnId": "turn-1", "excludeTurns": True}, bounded_fork
            for expected_id, decision in enumerate(
                    ("accept", "acceptForSession", "decline", "cancel"), 9001):
                started = await client.request("turn/start", {"threadId": "t1"})
                assert started == {"turn": {"id": "turn-3", "status": "inProgress"}}, started
                approval = await client.next_event()
                assert approval["id"] == expected_id, approval
                assert approval["method"] == "item/commandExecution/requestApproval", approval
                await client.respond_to_approval(approval["id"], decision)
                resolved = await client.next_event()
                assert resolved["method"] == "approval.resolved", resolved
                assert resolved["params"] == {"requestId": expected_id, "decision": decision}, resolved
            started = await client.start_turn("t2", "bounded handoff", "client-stable-1")
            assert started == {"turn": {"id": "turn-3", "status": "inProgress"}}, started
            approval = await client.next_event()
            assert approval["id"] == 9005 and approval["params"]["clientUserMessageId"] == "client-stable-1", approval
            assert approval["params"]["input"] == [{"type": "text", "text": "bounded handoff"}], approval
            await client.respond_to_approval(approval["id"], "accept")
            await client.next_event()
            await client.start_turn("t2", "needs-input", "client-input-1")
            questions = await client.next_event()
            assert questions["id"] == 9006 and questions["method"] == "item/tool/requestUserInput", questions
            assert questions["params"]["questions"][0]["options"][0]["label"] == "Option A", questions
            assert questions["params"]["questions"][1]["isOther"] is True, questions
            assert questions["params"]["questions"][2]["isSecret"] is True, questions
            await client.respond_to_user_input(questions["id"], {
                "q-choice": ["Option A"], "q-free": ["free answer"],
                "q-secret": ["dummy-secret-not-real"]})
            answered = await client.next_event()
            assert answered["method"] == "userInput.resolved", answered
            assert answered["params"] == {"requestId": 9006,
                "questionIds": ["q-choice", "q-free", "q-secret"], "answerCount": 3}, answered
            assert "dummy-secret-not-real" not in json.dumps(answered)
            for invalid in ({}, {"q-free": []}, {"q-free": [""]}):
                try:
                    await client.respond_to_user_input(9006, invalid)
                    raise AssertionError("空问答不应发送")
                except ValueError:
                    pass
            interrupted = await client.interrupt("t2", "turn-3")
            assert interrupted == {}, interrupted
            try:
                await client.resume_thread("")
                raise AssertionError("空 lastTurnId 不应 resume")
            except ValueError:
                pass
            for invalid_id, decision in ((None, "accept"), (True, "accept"), (9001, "approve")):
                try:
                    await client.respond_to_approval(invalid_id, decision)
                    raise AssertionError("非法审批 ID/决策不应发送")
                except ValueError:
                    pass
            try:
                await client.fork_thread("t1", "")
                raise AssertionError("空 lastTurnId 不应 fork")
            except ValueError:
                pass
            print("Codex JSON-RPC new/resume/fork/lastTurnId/approval/interrupt/events: OK")
        finally:
            await client.close()
        assert client.proc is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


asyncio.run(main())
