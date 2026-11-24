"""PTY 阻塞读不得耗尽 asyncio 默认线程池；占满旧池规模后仍能编译 LaTeX。"""
import asyncio
import json
import os
import time
import urllib.request

import aiohttp

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1").removesuffix("/api/v1")


def token():
    req = urllib.request.Request(B + "/api/v1/auth/login", method="POST",
        data=json.dumps({"email": "dev@vibeflow.cn", "password": "devpass123"}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["token"]


async def main():
    auth = token()
    headers = {"Authorization": f"Bearer {auth}"}
    owned_sessions = []
    async with aiohttp.ClientSession(headers=headers) as session:
        async def request(method, path, body=None):
            async with session.request(method, B + path, json=body) as response:
                return response.status, await response.json()

        try:
            status, case = await request("POST", "/api/v1/workspaces/1/cases",
                                         {"name": f"terminal-executor-{time.time_ns()}"})
            assert status == 200, case
            case_id = case["id"]
            tex = "\\documentclass{article}\n\\begin{document}\nPTY executor isolation.\n\\end{document}\n"
            status, written = await request("PUT", f"/api/v1/cases/{case_id}/file",
                                            {"path": "E-report/latex/main.tex", "content": tex})
            assert status == 200 and written.get("ok"), written
            count = min(32, (os.cpu_count() or 1) + 4)
            for _ in range(count):
                ws = await session.ws_connect(B + "/ws/terminal?cols=80&rows=24&shell=cmd&token=" + auth)
                ready = json.loads((await asyncio.wait_for(ws.receive(), 10)).data)
                owned_sessions.append(ready["session"])
                await ws.close()  # PTY 按重连契约继续存活，阻塞读仍运行
            print(f"1. 本次新建并保留 {len(owned_sessions)} 个 PTY（旧默认池规模）[OK]")

            status, job = await asyncio.wait_for(request("POST", f"/api/v1/cases/{case_id}/latex:build",
                {"main": "E-report/latex/main.tex", "recipe": "pdflatex"}), timeout=25)
            assert status == 200 and job.get("job_id"), (status, job)
            for _ in range(180):
                _, state = await request("GET", f"/api/v1/jobs/{job['job_id']}")
                if state["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
                    break
                await asyncio.sleep(0.5)
            assert state["status"] == "succeeded", state
            print(f"2. 默认池不再被 PTY 饿死，真实 LaTeX 作业 #{job['job_id']} 成功 [OK]")
        finally:
            for sid in owned_sessions:
                async with session.delete(f"{B}/api/v1/terminal/sessions/{sid}") as response:
                    assert response.status == 200, (sid, response.status, await response.text())
            print(f"3. 仅清理本次 {len(owned_sessions)} 个 PTY [OK]")


asyncio.run(main())
print("\n=== PTY / asyncio 默认池隔离冒烟通过 ===")
