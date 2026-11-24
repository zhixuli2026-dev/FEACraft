"""J21：50 用户 REST 并发与跨租户负载切片；不是完整 PTY/viz/Agent 验收。"""
import asyncio
import json
import os
import statistics
import time
from pathlib import Path
from uuid import uuid4

import httpx

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
USERS = int(os.environ.get("VIBEFLOW_TEST_USERS", "50"))


def timing(values):
    ordered = sorted(values)
    if not ordered:
        return {"requests": 0}
    percentile = lambda p: ordered[min(len(ordered) - 1, int(len(ordered) * p))]
    return {"requests": len(ordered), "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(percentile(0.95), 2), "p99_ms": round(percentile(0.99), 2),
            "max_ms": round(ordered[-1], 2)}


def report(summary):
    if path := os.environ.get("VIBEFLOW_TEST_LOAD_REPORT"):
        Path(path).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("LOAD50_SUMMARY", summary)


async def steady_reads():
    """Measure dashboard reads after preparing actual accounts and cases."""
    latencies = {"me": [], "own_tree": []}
    errors = []
    ready = asyncio.Semaphore(4)
    rounds = 6
    async with httpx.AsyncClient(base_url=B, timeout=30, trust_env=False) as client:
        async def prepare(index):
            async with ready:
                email = f"read50-{index}-{uuid4().hex}@example.com"
                response = await client.post("/auth/register", json={
                    "email": email, "name": "Read load user", "password": "read50-pass-123",
                })
                response.raise_for_status()
                auth = {"Authorization": "Bearer " + response.json()["token"]}
                workspace = await client.post("/workspaces", json={"name": "Read load"}, headers=auth)
                workspace.raise_for_status()
                case = await client.post(f"/workspaces/{workspace.json()['id']}/cases",
                                         json={"name": "Read load case"}, headers=auth)
                case.raise_for_status()
                return email, case.json()["id"], auth

        users = await asyncio.gather(*(prepare(index) for index in range(USERS)))
        started_all = time.perf_counter()

        async def read_account_and_tree(index, user):
            email, case_id, auth = user
            requests = (("me", "/auth/me"), ("own_tree", f"/cases/{case_id}/tree"))
            for stage, path in requests:
                started = time.perf_counter()
                try:
                    response = await client.get(path, headers=auth)
                    latencies[stage].append((time.perf_counter() - started) * 1000)
                    if response.status_code != 200:
                        errors.append((index, stage, response.status_code))
                    elif stage == "me" and response.json().get("email") != email:
                        errors.append((index, stage, "account_mismatch"))
                    elif stage == "own_tree" and not response.json().get("entries"):
                        errors.append((index, stage, "empty_tree"))
                except (httpx.HTTPError, ValueError) as error:
                    errors.append((index, stage, type(error).__name__))

        for round_index in range(rounds):
            if round_index:
                await asyncio.sleep(.1)
            await asyncio.gather(*(read_account_and_tree(index, user) for index, user in enumerate(users)))

    summary = {"scenario": "steady_reads", "users": USERS, "rounds": rounds, "errors": errors,
               "actual_model_calls": 0, "elapsed_s": round(time.perf_counter() - started_all, 3),
               **timing(value for values in latencies.values() for value in values),
               "by_operation": {stage: timing(values) for stage, values in latencies.items()}}
    report(summary)
    assert not errors, errors[:10]
    assert summary["requests"] == USERS * 2 * rounds, summary["requests"]


async def main():
    latencies = {stage: [] for stage in ("register", "workspace", "case", "me", "tree")}
    errors = []

    async with httpx.AsyncClient(base_url=B, timeout=30, trust_env=False) as client:
        # Use a known existing case with a different owner for every negative read.
        run_id = uuid4().hex
        control = await client.post("/auth/register", json={
            "email": f"load50-control-{run_id}@example.com",
            "name": "Load control owner",
            "password": "load50-pass-123",
        })
        control.raise_for_status()
        control_auth = {"Authorization": "Bearer " + control.json()["token"]}
        workspace = await client.post("/workspaces", json={"name": "Load control"}, headers=control_auth)
        workspace.raise_for_status()
        case = await client.post(f"/workspaces/{workspace.json()['id']}/cases",
                                 json={"name": "Load control case"}, headers=control_auth)
        case.raise_for_status()
        control_id = case.json()["id"]
        tree = await client.get(f"/cases/{control_id}/tree", headers=control_auth)
        tree.raise_for_status()

        started_all = time.perf_counter()

        async def request(stage, method, path, body=None, token=None):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            started = time.perf_counter()
            response = await client.request(method, path, json=body, headers=headers)
            latencies[stage].append((time.perf_counter() - started) * 1000)
            return response

        async def user_flow(index):
            try:
                email = f"load50-{index}-{time.time_ns()}@vibeflow.cn"
                response = await request("register", "POST", "/auth/register", {
                    "email": email, "name": f"load-{index}", "password": "load50-pass-123"})
                if response.status_code != 200:
                    errors.append((index, "register", response.status_code))
                    return
                token = response.json()["token"]
                response = await request("workspace", "POST", "/workspaces",
                                         {"name": f"load50-{index}-{time.time_ns()}"}, token)
                if response.status_code != 200:
                    errors.append((index, "workspace", response.status_code))
                    return
                workspace_id = response.json()["id"]
                response = await request("case", "POST", f"/workspaces/{workspace_id}/cases",
                                         {"name": f"load50-case-{index}"}, token)
                if response.status_code != 200:
                    content_type = response.headers.get("content-type", "")
                    detail = response.json().get("detail") if content_type.startswith("application/json") else None
                    code = detail.get("code") if isinstance(detail, dict) else None
                    errors.append((index, "case", response.status_code, code))
                    return
                case_id = response.json()["id"]
                response = await request("me", "GET", "/auth/me", token=token)
                if response.status_code != 200:
                    errors.append((index, "me", response.status_code))
                    return
                target = control_id if index else case_id
                response = await request("tree", "GET", f"/cases/{target}/tree", token=token)
                expected = 200 if index == 0 else 404
                if response.status_code != expected:
                    errors.append((index, "isolation", response.status_code))
            except Exception as error:
                errors.append((index, "exception", type(error).__name__))

        await asyncio.gather(*(user_flow(index) for index in range(USERS)))

    summary = {"users": USERS, "errors": errors, "actual_model_calls": 0,
               "elapsed_s": round(time.perf_counter() - started_all, 3),
               **timing(value for values in latencies.values() for value in values),
               "by_operation": {stage: timing(values) for stage, values in latencies.items()}}
    report(summary)
    assert not errors, errors[:10]
    assert summary["requests"] == USERS * 5, summary["requests"]
    print("J21 50-user concurrent REST and cross-tenant isolation: OK")


asyncio.run(steady_reads() if os.environ.get("VIBEFLOW_TEST_LOAD_SCENARIO") == "steady_reads" else main())
