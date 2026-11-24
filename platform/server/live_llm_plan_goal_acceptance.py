"""Real DeepSeek tools and Edge for the persisted session plan and goal."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


CASE = "Plan and goal acceptance"
SESSION = "Actual plan and goal"
OTHER = "Independent conversation"
PLAN = [
    {"title": "确定验收对象", "status": "completed"},
    {"title": "核对网格编号", "status": "in_progress"},
    {"title": "记录独立复核", "status": "pending"},
]
FINAL_PLAN = [{**step, "status": "completed"} for step in PLAN]
CRITERIA = ["确认隔离算例的网格编号", "留存复核记录"]
BUDGET = {"max_turns": 4, "max_tool_calls": 12}
STOP = "指定网格记录已完成复核"
INVALID_PLAN = [{"title": name, "status": "in_progress"} for name in ("未授权的第一项", "未授权的第二项")]


OBSERVE = '''
from app.services import process_identity_native
(root/'api-birth.json').write_text(json.dumps(process_identity_native.observe(__import__('os').getpid())))
tracked_send = httpx.AsyncClient.send

async def witness_goal_prompt(self, request, *args, **kwargs):
    if request.url.host == 'api.deepseek.com' and request.url.path.endswith('/chat/completions'):
        expected = (root/'expected-objective.txt').read_text(encoding='utf8')
        messages = json.loads(request.content).get('messages', [])
        system = '\\n'.join(item['content'] for item in messages if item.get('role') == 'system'
                          and isinstance(item.get('content'), str))
        receipt({'kind': 'goal_context', 'has_objective': expected in system,
                 'has_active_budget': 'max_turns=4, max_tool_calls=12' in system})
    return await tracked_send(self, request, *args, **kwargs)

httpx.AsyncClient.send = witness_goal_prompt
'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def goal_value(objective, status="active", reason=""):
    return {"objective": objective, "acceptance_criteria": CRITERIA,
            "budget": BUDGET, "status": status, "stop_reason": reason}


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings["root"])
    phase = settings["phase"]
    base = settings["base"]
    conversation = settings["conversation"]
    prompt_objective = settings["objective"]
    result = {"passed": False, "turns": [], "phase": phase}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings["channel"])
        context = edge.new_context(viewport={"width": 1550, "height": 1010}, locale="zh-CN")
        context.add_init_script("if(location.origin===" + json.dumps(base) + ")localStorage.setItem('vf_token',"
                                + json.dumps(settings["token"]) + ");")
        page = context.new_page()
        page.set_default_timeout(80000)
        expect.set_options(timeout=80000)
        choose = page.get_by_test_id("agent-conversation")
        draft = page.get_by_test_id("agent-input")
        send = page.get_by_test_id("agent-send")
        plan = page.get_by_test_id("plan-strip")
        goal = page.get_by_test_id("goal-strip")
        writes = []

        def watch(request):
            path = request.url.removeprefix(base)
            if request.method == "POST" and path.startswith("/api/v1/conversations/"):
                writes.append(path)

        def open_panel(cid):
            case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(case).to_be_visible()
            if case.get_attribute("aria-pressed") != "true":
                case.click()
            if not choose.is_visible():
                page.locator(".dv-tab:visible").filter(has_text=re.compile(r"^Agent$")).click()
            expect(choose).to_be_enabled()
            if choose.input_value() != str(cid):
                choose.select_option(str(cid))
            expect(choose).to_have_value(str(cid))
            expect(draft).to_be_enabled()

        def plan_matches(steps):
            rows = plan.get_by_role("listitem")
            expect(rows).to_have_count(len(steps))
            for index, step in enumerate(steps):
                expect(rows.nth(index)).to_contain_text(step["title"])
                expect(rows.nth(index)).to_have_attribute("data-status", step["status"])

        def goal_matches(status, revision):
            expect(goal).to_have_attribute("data-status", status)
            expect(goal).to_have_attribute("data-revision", str(revision))
            expect(goal).to_contain_text(prompt_objective)
            expect(goal).to_contain_text("4 轮/12 次工具调用")
            for item in CRITERIA:
                expect(goal).to_contain_text(item)

        def turn(prompt):
            endpoint = f"/api/v1/conversations/{conversation}/messages"
            draft.fill(prompt)
            with page.expect_response(lambda response: response.url == base + endpoint and response.request.method == "POST") as received:
                send.click()
            response = received.value
            assert response.status == 200, (response.status, response.text()[:350])
            expect(draft).to_be_enabled()
            frames = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith("data:")]
            assert any(event["type"] == "run.completed" for event in frames), [e["type"] for e in frames]
            assert not any(event["type"] in {"goal.progress", "request.failed", "request.timed_out", "stream.error"} for event in frames)
            outcomes = {event["tool"]: event for event in frames if event["type"] == "tool.result"}
            assert outcomes.keys() == {"update_plan", "update_goal"}, list(outcomes)
            revisions = [event["goal"]["revision"] for event in frames if event["type"] == "goal.updated"]
            result["turns"].append({"outcomes":outcomes,"goal_revisions":revisions})
            return outcomes, revisions

        try:
            page.on("request", watch)
            page.goto(base, wait_until="domcontentloaded")
            open_panel(conversation)
            if phase == "create":
                expect(plan).to_have_count(0)
                expect(goal).to_have_count(0)
                prompt = ("这是隔离算例的工具验收。请实际调用且只各调用一次 update_plan 和 update_goal，"
                          "工具参数严格复制以下 JSON，完成后只回复「登记完毕」。\n"
                          "update_plan: " + encoded({"steps":PLAN}) + "\nupdate_goal: " + encoded(goal_value(prompt_objective)))
                outcomes, revisions = turn(prompt)
                assert all(event["ok"] for event in outcomes.values()) and revisions == [1]
                plan_matches(PLAN)
                goal_matches("active", 1)
                expect(page.get_by_test_id("goal-run")).to_be_enabled()
                page.screenshot(path=str(root / "plan-goal-active.png"))
                page.reload(wait_until="domcontentloaded")
                open_panel(conversation)
                plan_matches(PLAN)
                goal_matches("active", 1)
            else:
                plan_matches(PLAN)
                goal_matches("active", 1)
                prompt = ("这是应用校验失败测试。请只各实际发出一次故意非法的 update_plan 与 update_goal 请求。"
                          "复制 JSON，不要修正、不要重试、不要调用其它工具。收到工具报错后只回复「拒绝已观察」。\n"
                          "update_plan: " + encoded({"steps":INVALID_PLAN}) + "\nupdate_goal: "
                          + encoded(goal_value(prompt_objective, status="completed")))
                outcomes, revisions = turn(prompt)
                assert not any(event["ok"] for event in outcomes.values()) and not revisions
                assert "最多只能有一个 in_progress" in outcomes["update_plan"]["result"]
                assert "终态必须提供 stop_reason" in outcomes["update_goal"]["result"]
                plan_matches(PLAN)
                goal_matches("active", 1)
                prompt = ("现在完成隔离验收。请实际各调用一次 update_plan 和 update_goal，不调用其它工具。"
                          "update_plan 的参数是 " + encoded({"steps":FINAL_PLAN}) + "。"
                          "update_goal 请从当前会话持久目标精确复用 objective、acceptance_criteria 和 budget，"
                          "status 设 completed，stop_reason 精确填「" + STOP + "」。不要新增验收条件。然后只回复「完成复核」。")
                assert prompt_objective not in prompt
                outcomes, revisions = turn(prompt)
                assert all(event["ok"] for event in outcomes.values()) and revisions == [2]
                plan_matches(FINAL_PLAN)
                goal_matches("completed", 2)
                expect(goal).to_contain_text(STOP)
                expect(page.get_by_test_id("goal-run")).to_have_count(0)
                page.screenshot(path=str(root / "plan-goal-completed.png"))
                open_panel(settings["other"])
                expect(plan).to_have_count(0)
                expect(goal).to_have_count(0)
                open_panel(conversation)
                plan_matches(FINAL_PLAN)
                goal_matches("completed", 2)
            assert len(writes) == (1 if phase == "create" else 2)
            assert all(path.endswith("/messages") for path in writes)
            result["passed"] = True
        finally:
            result["conversation_posts"] = writes
            (root / (phase + "-browser.json")).write_text(encoded(result), encoding="utf8")
            if not result["passed"]:
                page.screenshot(path=str(root / (phase + "-failure.png")))
            edge.close()


def listener_closed(port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as client:
            client.settimeout(.3)
            if client.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(.08)
    raise RuntimeError("Isolated API still owns its listening port")


def verify(args, config):
    from app.services.process_identity_native import probe

    root_api = IsolatedServer("live-plan-goal", app="plan_goal_fixture:app")
    root = root_api.root
    dist = args.dist.resolve(strict=True)
    objective = "复核独立算例网格 " + uuid.uuid4().hex[:9]
    (root / "expected-objective.txt").write_text(objective, encoding="utf8")
    temp = root / "browser-tmp"
    temp.mkdir()
    (root / "plan_goal_fixture.py").write_text(FIXTURE + OBSERVE
        + "\nfrom fastapi.staticfiles import StaticFiles\napp.mount('/', StaticFiles(directory="
        + repr(str(dist)) + ", html=True))\n", encoding="utf8")
    result = {"passed":False,"model":config["model"],"provider":"deepseek","host":"api.deepseek.com","mock_llm_calls":0}
    print("LIVE_PLAN_GOAL_EVIDENCE", root, flush=True)

    def run_browser(phase):
        params = {"root":str(root),"base":root_api.base,"token":root_api.token,"objective":objective,
                  "phase":phase,"channel":args.channel,"conversation":conversation,"other":other}
        subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
            input=encoded(params), text=True, encoding="utf8", check=True, timeout=240,
            env=dict(os.environ, TMP=str(temp), TEMP=str(temp), PYTHONIOENCODING="utf8"))
        return json.loads((root / (phase + "-browser.json")).read_text(encoding="utf8"))

    try:
        with root_api as api:
            case = api.create_case(CASE)
            owner = api.call("GET", "/auth/me")
            profile = api.call("POST", "/provider-profiles", {"provider":"deepseek", "name":"Live plan goal",
                "default_model":config["model"],"base_url":config["base_url"],"api_key":config["key"]})
            values = {"case_id":case["id"],"provider_profile_id":profile["id"],"model":config["model"],"permission_mode":"plan"}
            conversation = api.call("POST", "/conversations", {**values,"title":SESSION})["id"]
            other = api.call("POST", "/conversations", {**values,"title":OTHER})["id"]
            endpoint = f"/conversations/{conversation}"
            result["create"] = run_browser("create")
            active = api.call("GET", endpoint+"?limit=1")
            assert active["current_plan"] == PLAN
            assert active["current_goal"] == {**goal_value(objective),"goal_id":f"session-{conversation}","revision":1}
            first_birth = json.loads((root / "api-birth.json").read_text(encoding="utf8"))
        listener_closed(root_api.port)
        assert probe(first_birth)["state"] == "exited"
        (root / "first-api.log").write_bytes((root / "api.log").read_bytes())
        as_before = (root / "upstream.jsonl").read_text(encoding="utf8").splitlines()
        root_api.token = None
        with root_api as api:
            second_birth = json.loads((root / "api-birth.json").read_text(encoding="utf8"))
            assert second_birth != first_birth
            api.token = api.call("POST", "/auth/login", {"email":owner["email"], "password":"isolated-test-123"})["token"]
            cold = api.call("GET", endpoint+"?limit=1")
            assert cold["current_goal"] == active["current_goal"] and cold["current_plan"] == PLAN
            result["resumed"] = run_browser("resume")
            settled = api.call("GET", endpoint)
            assert settled["current_goal"] == {**goal_value(objective, "completed", STOP),
                                                "goal_id":f"session-{conversation}","revision":2}
            assert settled["current_plan"] == FINAL_PLAN
            stored_goal = [event for event in settled["events"] if event["type"] == "goal"]
            assert [event["payload"]["revision"] for event in stored_goal] == [1,2]
            result["durable_goal"] = settled["current_goal"]
            assert api.call("GET", f"/conversations/{other}")["current_goal"] is None
            api.create_case("Plan goal outside account")
            api.call("GET", endpoint, expect=404)
        listener_closed(root_api.port)
        assert probe(second_birth)["state"] == "exited"
        receipts = [json.loads(line) for line in (root / "upstream.jsonl").read_text(encoding="utf8").splitlines()]
        restored_request = next(record for record in receipts[len(as_before):] if record["kind"] == "goal_context")
        assert restored_request["has_objective"] and restored_request["has_active_budget"]
        http = [record for record in receipts if record["kind"] == "http"]
        assert len(http) >= 3 and all(record["status"] == 200 and record["host"] == "api.deepseek.com" for record in http)
        identities = [record for record in receipts if record["kind"] == "stream_identity"]
        assert len({record["id"] for record in identities}) >= 3
        result.update(passed=True, real_model_requests=len(http), actual_model_identities=identities,
                      cold_restore=True, other_account_denied=True, both_api_births_exited=True)
    finally:
        (root / '.vault_key').unlink(missing_ok=True)
        (root / "live-plan-goal-result.json").write_text(encoded(result), encoding="utf8")
    print("LIVE_PLAN_GOAL_PASS", root, flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        browser(json.load(sys.stdin))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--source-profile-id", required=True, type=int)
        parser.add_argument("--dist", required=True, type=Path)
        parser.add_argument("--browser-python", required=True, type=Path)
        parser.add_argument("--channel", default="msedge")
        arguments = parser.parse_args()
        verify(arguments, asyncio.run(source(arguments.source_profile_id)))
