"""受约束的 SSH executionProfile：host allowlist、密钥与 known_hosts。"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import shlex
import time

import asyncssh


def _allowed(host: str):
    hosts = {item.strip() for item in os.getenv("VIBEFLOW_REMOTE_HOSTS", "").split(",") if item.strip()}
    if not hosts or host not in hosts:
        raise ValueError("远程主机不在 VIBEFLOW_REMOTE_HOSTS allowlist")


@asynccontextmanager
async def connect(profile):
    host = profile["host"]
    _allowed(host)
    client_key = Path(profile["client_key"])
    known_hosts = Path(profile["known_hosts"])
    if not client_key.is_file() or not known_hosts.is_file():
        raise ValueError("client_key/known_hosts 必须存在")
    async with asyncssh.connect(host, int(profile.get("port", 22)),
                                username=profile["username"], client_keys=[str(client_key)],
                                known_hosts=str(known_hosts), agent_path=None) as conn:
        yield conn


async def run_remote(profile: dict, command: str) -> dict:
    _allowed(profile["host"])
    if not command.strip() or len(command) > 4000:
        raise ValueError("远程命令长度必须为 1–4000")
    timeout = profile.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise ValueError("远程超时必须为 1–3600 秒")
    started = time.perf_counter()
    async with connect(profile) as conn:
        workdir = profile.get("workdir", ".")
        result = await conn.run(f"cd {shlex.quote(workdir)} && {command}", timeout=timeout,
                                term_type="dumb", term_size=(160, 40))
    return {"exit_status": result.exit_status, "stdout": result.stdout[-16000:],
            "stderr": result.stderr[-4000:], "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
