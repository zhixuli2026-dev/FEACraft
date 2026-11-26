"""Verify API goal auto-continuation and limits with genuine DeepSeek calls."""

import argparse
import asyncio
import json
from pathlib import Path
import socket
import sqlite3
import time
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


OBSERVE = '''
import asyncio
import os
import anyio
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import Conversation, Plan
from app.routers.agent import _append_goal_revision, _latest_goal, goal_payload
from app.routers.workspaces import get_owned_case
from app.services import process_identity_native

(root/'api-birth.json').write_text(json.dumps(process_identity_native.observe(os.getpid())))

def stage():
    return (root/'stage.txt').read_text(encoding='utf8')

tracked_send = httpx.AsyncClient.send
async def signal_actual_receipt(self, request, *args, **kwargs):
    response = await tracked_send(self, request, *args, **kwargs)
    if request.url.host == 'api.deepseek.com' and request.url.path.endswith('/chat/completions'):
        selected = stage()
        receipt({'kind':'goal_http','stage':selected,'status':response.status_code})
        if selected in {'cancel','disconnect'}:
            assert response.status_code == 200
            (root/('active-'+selected)).touch()
            try:
                await asyncio.Event().wait()  # hold an actual accepted request, never supply response data
            finally:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
                (root/('closed-'+selected)).touch()
    return response
httpx.AsyncClient.send = signal_actual_receipt

tracked_lines = httpx.Response.aiter_lines
async def observe_real_tools(self, *args, **kwargs):
    calls = {}
    actual = self.request.url.host == 'api.deepseek.com'
    selected = stage() if actual else None
    try:
        async for line in tracked_lines(self, *args, **kwargs):
            if actual and line.startswith('data:') and line[5:].strip() != '[DONE]':
                try:
                    message = json.loads(line[5:])
                    for choice in message.get('choices', []):
                        for part in choice.get('delta', {}).get('tool_calls', []) or []:
                            name = part.get('function', {}).get('name')
                            if name:
                                calls[str(part['index'])] = name
                except (ValueError, TypeError, KeyError):
                    pass
            yield line
    finally:
        if actual and calls:
            receipt({'kind':'actual_tool_choices','stage':selected,'names':list(calls.values())})
httpx.Response.aiter_lines = observe_real_tools

# These endpoints exist only inside a new isolated acceptance server. They
# configure preconditions, leaving every actual inference to the provider.
@app.post('/fixture/goal/{cid}')
async def set_initial_goal(cid:int, body:dict, user=Depends(get_current_user), db=Depends(get_db)):
    conversation = await db.get(Conversation,cid)
    await get_owned_case(conversation.case_id,user,db,write=True)
    assert await _latest_goal(db,cid) is None
    initial = await _append_goal_revision(db,cid,goal_payload(body))
    await db.commit()
    return initial

@app.post('/fixture/close-quota')
async def close_quota(user=Depends(get_current_user), db=Depends(get_db)):
    plan = await db.scalar(select(Plan).where(Plan.code=='free'))
    plan.quotas = json.dumps({**json.loads(plan.quotas),'llm_msgs_per_day':0})
    await db.commit()
    return {'ready':True}
'''


def wait_until(operation, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = operation()
        if result:
            return result
        time.sleep(.08)
    raise TimeoutError("The actual owner or provider did not reach the verification boundary")


def events(response):
    if response.status_code != 200:
        response.read()
        detail = response.json().get("detail")
        if isinstance(detail, list):
            detail = [{key:item.get(key) for key in ("loc","msg","type")} for item in detail]
        raise AssertionError((response.status_code, detail))
    return [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]


def status_frames(frames):
    keep = {"goal.progress", "goal.updated", "run.completed", "run.cancelled", "stream.error", "request.failed"}
    return [frame for frame in frames if frame["type"] in keep]


def goal_rounds(frames):
    return [frame["turn"] for frame in frames if frame["type"] == "goal.progress"]


def live_owner(root, cid):
    target = (root / "test.sqlite").resolve().as_uri()+"?mode=ro"
    with sqlite3.connect(target, uri=True) as db:
        return db.execute("SELECT count(*) FROM conversation_executions WHERE conversation_id=? AND phase='active'", (cid,)).fetchone()[0]


def execute(args, config):
    from app.services.process_identity_native import probe

    server = IsolatedServer("live-goal-runner", app="goal_runner_fixture:app")
    root = server.root
    observer = "live_request_limit = 16\n" + FIXTURE + OBSERVE
    (root / "goal_runner_fixture.py").write_text(observer, encoding="utf8")
    report = {"passed":False,"service":"api.deepseek.com","model":config["model"],"mock_llm_calls":0}
    print("LIVE_GOAL_RUNNER_EVIDENCE", root, flush=True)

    def select_stage(name):
        (root / "stage.txt").write_text(name, encoding="utf8")

    def persist():
        trace = root/"upstream.jsonl"
        observed = [json.loads(line) for line in trace.read_text(encoding="utf8").splitlines()] if trace.exists() else []
        report["model_http"] = [row for row in observed if row["kind"] == "goal_http"]
        report["actual_tool_choices"] = [row for row in observed if row["kind"] == "actual_tool_choices"]
        report["usage"] = [row for row in observed if row["kind"] == "stream_identity"]
        (root/"live-goal-runner-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf8")

    try:
        with server as api:
            birth = json.loads((root/"api-birth.json").read_text(encoding="utf8"))
            case = api.create_case("Actual API goals")
            profile = api.call("POST", "/provider-profiles", {"provider":"deepseek", "name":"Goal runner actual provider",
                "api_key":config["key"],"base_url":config["base_url"],"default_model":config["model"]})
            for name in ("one", "two"):
                api.put(f"Ref/{name}.txt", "read-only goal fixture " + uuid.uuid4().hex)

            def make(name, mode, objective=None, turns=4, tools=8):
                cid = api.call("POST", "/conversations", {"case_id":case["id"],"title":name,"permission_mode":mode,
                            "provider_profile_id":profile["id"],"model":config["model"]})["id"]
                if objective:
                    api.call("POST", f"/fixture/goal/{cid}", {"objective":objective,"budget":{
                        "max_turns":turns,"max_tool_calls":tools}}, api_prefix=False)
                return cid

            def detail(cid):
                return api.call("GET", f"/conversations/{cid}")

            client_args={"base_url":api.base+"/api/v1","headers":{"Authorization":"Bearer "+api.token},
                         "timeout":httpx.Timeout(105.0),"trust_env":False}
            with httpx.Client(**client_args) as client:
                def run(cid, body=None):
                    with client.stream("POST",f"/conversations/{cid}/goal:run",json=body or {}) as response:
                        return events(response)

                cid = make("Actual adaptive completion", "plan")
                marker = uuid.uuid4().hex[:10]
                objective = (f"核对隔离算例 {marker}。自动第 1 轮只回复 STEP_ONE，不调用任何工具；"
                    "自动第 2 轮必须调用 update_goal，将当前完整 objective 原样复制，状态设 completed，"
                    "stop_reason 填 TWO_ROUNDS_VERIFIED，预算保持。调用成功后回复 STEP_TWO。"
                    "以最新用户消息的‘第 X/Y 轮’为准，第一轮禁止结束目标。")
                payload={"objective":objective,"budget":{"max_turns":3,"max_tool_calls":8},"status":"active"}
                prompt = "请实际调用 update_goal 登记以下参数。现在只登记，保持 active，不要执行 objective，登记后回复 READY。\n"
                select_stage("register")
                message = {"content":prompt+json.dumps(payload,ensure_ascii=False),"provider_profile_id":profile["id"],"model":config["model"]}
                with client.stream("POST",f"/conversations/{cid}/messages",json=message) as response:
                    registered = events(response)
                assert any(e["type"]=="tool.result" and e.get("tool")=="update_goal" and e["ok"] for e in registered)
                start = detail(cid)["current_goal"]
                assert start["revision"]==1 and start["status"]=="active" and start["objective"]==objective
                select_stage("complete")
                completed = run(cid)
                assert goal_rounds(completed)==[1,2],status_frames(completed)
                record=detail(cid)["current_goal"]
                assert record["status"]=="completed" and record["revision"]==2 and record["goal_id"]==start["goal_id"]
                assert record["stop_reason"]=="TWO_ROUNDS_VERIFIED" and record["objective"]==objective
                assert live_owner(root,cid)==0
                api.call("POST",f"/conversations/{cid}/goal:run",{},expect=409)
                report["completion"]={"rounds":[1,2],"goal":record,"frames":status_frames(completed)}
                persist()

                cid_budget=make("Actual tool guard","plan",
                    "验收工具预算。开始时必须在同一条回复中并行实际调用两次 read_text_file，"
                    "参数 path 依次是 Ref/one.txt 和 Ref/two.txt。两次都发，不要改成只读取一个。"
                    "这是明示拒绝测试，勿调用其他工具，勿修改文件。",tools=1)
                select_stage("tools")
                budget = run(cid_budget)
                assert goal_rounds(budget)==[1] and any(e.get("code")=="goal_tool_budget" for e in budget),status_frames(budget)
                denied = detail(cid_budget)
                assert denied["current_goal"]["status"]=="blocked" and "工具预算 1" in denied["current_goal"]["stop_reason"]
                assert not denied["tool_calls"] and not any(e["type"].startswith("tool_") for e in denied["events"])
                assert not any(e["payload"].get("tool_calls") for e in denied["events"] if e["type"]=="message")
                assert live_owner(root,cid_budget)==0
                report["tools"]={"goal":denied["current_goal"],"dispatched":0,"frames":status_frames(budget)}

                cid_turn=make("Actual turn budget","ask","每轮只回复 STEP_ONLY。保留活动目标。",turns=4)
                select_stage("turn")
                limited=run(cid_turn,{"max_turns":1})
                assert goal_rounds(limited)==[1] and any(e["type"]=="run.completed" for e in limited)
                record=detail(cid_turn)["current_goal"]
                assert record["status"]=="blocked" and "轮次预算 1" in record["stop_reason"] and live_owner(root,cid_turn)==0
                report["turn"]={"goal":record,"frames":status_frames(limited)}

                for stage in ("cancel","disconnect"):
                    interrupted = make("Actual "+stage,"ask","只输出一句仿真说明，等待取消验证。")
                    select_stage(stage)
                    with client.stream("POST",f"/conversations/{interrupted}/goal:run",json={}) as response:
                        assert response.status_code==200
                        wait_until((root/("active-"+stage)).exists)
                        assert live_owner(root,interrupted)==1
                        api.call("POST",f"/conversations/{interrupted}/goal:run",{},expect=409)
                        if stage=="cancel":
                            assert api.call("POST",f"/conversations/{interrupted}/messages:cancel",{})["ok"]
                            frames=events(response)
                            assert goal_rounds(frames)==[1] and any(e["type"]=="run.cancelled" for e in frames)
                        else:
                            response.close()
                    wait_until((root/("closed-"+stage)).exists)
                    wait_until(lambda:live_owner(root,interrupted)==0)
                    stopped=detail(interrupted)
                    assert stopped["current_goal"]["status"]=="cancelled" and stopped["current_goal"]["revision"]==2
                    assert len([e for e in stopped["events"] if e["type"]=="message" and e["payload"].get("role")=="user"])==1
                    report[stage]={"goal":stopped["current_goal"],"real_response_closed":True,"owner_released":True}
                    persist()

                cid_quota=make("Live quota refusal","ask","当配额耗尽时保护已有目标。")
                assert api.call("POST","/fixture/close-quota",{},api_prefix=False)["ready"]
                select_stage("quota")
                limited=run(cid_quota)
                record=detail(cid_quota)["current_goal"]
                assert not goal_rounds(limited) and record["status"]=="blocked" and "配额" in record["stop_reason"]
                assert not [e for e in detail(cid_quota)["events"] if e["type"]=="message"]
                assert live_owner(root,cid_quota)==0
                report["quota"]={"goal":record,"provider_admission_skipped":True}

            api.create_case("Actual goal access denied")
            api.call("POST",f"/conversations/{cid}/goal:run",{},expect=404)
        wait_until(lambda:probe(birth)["state"]=="exited")
        with socket.socket() as sock:
            sock.settimeout(.5)
            assert sock.connect_ex(("127.0.0.1",server.port))!=0
        persist()
        assert report["model_http"] and all(item["status"]==200 for item in report["model_http"])
        assert not any(item["stage"]=="quota" for item in report["model_http"])
        assert any(item["stage"]=="tools" and item["names"].count("read_text_file")==2
                   for item in report["actual_tool_choices"])
        report.update(passed=True,api_exited=True,foreign_account_status=404)
    finally:
        (root/".vault_key").unlink(missing_ok=True)
        persist()
    print("LIVE_GOAL_RUNNER_PASS", root, flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-profile-id",type=int,required=True)
    args=parser.parse_args()
    execute(args,asyncio.run(source(args.source_profile_id)))
