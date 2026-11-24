"""实库迁移前备份/临时库恢复校验；凭据只从当前环境读取，不打印。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import unquote, urlparse

from app.config import get_settings

TOOLS = Path(r"C:\Program Files\PostgreSQL\16\bin")
BACKUP_ROOT = Path(__file__).resolve().parents[1] / "backups"
REQUIRED_TABLES = {"users", "workspaces", "cases", "jobs", "conversations", "agent_events"}


def run(argv: list[str], env: dict) -> str:
    result = subprocess.run(argv, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
    if result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): "
                           f"{(result.stderr or result.stdout)[-1000:]}")
    return result.stdout


def main():
    settings = get_settings()
    url = urlparse(settings.database_url)
    if url.scheme not in {"postgresql", "postgresql+asyncpg"}:
        raise RuntimeError("实库迁移备份只支持 PostgreSQL")
    env = dict(os.environ)
    env["PGPASSWORD"] = unquote(url.password or "")
    env["PGCLIENTENCODING"] = "UTF8"
    host = url.hostname or "localhost"
    port = str(url.port or 5432)
    user = unquote(url.username or "")
    database = unquote(url.path.lstrip("/"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = BACKUP_ROOT / stamp
    directory.mkdir(parents=True, exist_ok=False)
    backup = directory / "vibeflow.pgdump"
    run([str(TOOLS / "pg_dump.exe"), "--format=custom", "--no-owner", "--no-privileges",
         "--host", host, "--port", port, "--username", user,
         "--dbname", database, "--file", str(backup)], env)
    listing = run([str(TOOLS / "pg_restore.exe"), "--list", str(backup)], env)
    missing = sorted(table for table in REQUIRED_TABLES if f"TABLE public {table}" not in listing)
    if missing:
        raise RuntimeError(f"备份缺少核心表: {missing}")

    temporary = f"vibeflow_restore_{int(time.time())}"
    try:
        run([str(TOOLS / "psql.exe"), "--host", host, "--port", port, "--username", user,
             "--dbname", "postgres", "--set", "ON_ERROR_STOP=1", "--command",
             f'CREATE DATABASE "{temporary}" TEMPLATE template0'], env)
        run([str(TOOLS / "pg_restore.exe"), "--no-owner", "--no-privileges", "--exit-on-error",
             "--host", host, "--port", port, "--username", user,
             "--dbname", temporary, str(backup)], env)
        restored = run([str(TOOLS / "psql.exe"), "--host", host, "--port", port,
                        "--username", user, "--dbname", temporary, "--tuples-only",
                        "--command", "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public';"], env)
        if int(restored.strip()) < len(REQUIRED_TABLES):
            raise RuntimeError("临时恢复库核心表数量不足")
        manifest = {"schema": "vibeflow.postgres-backup-check.v1",
                    "created_at": time.time(), "database": database,
                    "backup_file": backup.name, "bytes": backup.stat().st_size,
                    "required_tables": sorted(REQUIRED_TABLES), "restore_validated": True,
                    "temporary_tables": int(restored.strip())}
        (directory / "restore-check.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"BACKUP_RESTORE_OK {directory} bytes={backup.stat().st_size} tables={restored.strip()}")
    finally:
        run([str(TOOLS / "psql.exe"), "--host", host, "--port", port, "--username", user,
             "--dbname", "postgres", "--set", "ON_ERROR_STOP=1", "--command",
             f'DROP DATABASE IF EXISTS "{temporary}"'], env)


if __name__ == "__main__":
    main()
