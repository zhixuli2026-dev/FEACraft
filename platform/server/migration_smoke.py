"""Alembic 基线冒烟：仅操作临时 SQLite，不接触开发 PostgreSQL。"""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
EXPECTED = {
    "users", "workspaces", "cases", "provider_profiles", "conversations",
    "agent_events", "tool_calls", "jobs", "organizations", "memberships",
    "plans", "subscriptions",
    "workflow_runs", "workflow_steps", "job_attempts", "job_execution_heads",
    "artifact_publications", "artifact_publication_heads", "case_event_counters",
    "durable_events", "event_outbox",
}


def run_alembic(revision, env):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", revision[0], revision[1]],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise AssertionError(f"alembic {revision} failed: {result.returncode}")
    return result


def tables(db_path):
    con = sqlite3.connect(db_path)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        con.close()


def main():
    with tempfile.TemporaryDirectory(prefix="vibeflow-migration-") as tmp:
        db_path = Path(tmp) / "migration.db"
        env = os.environ.copy()
        env["VIBEFLOW_DATABASE_URL"] = "sqlite+aiosqlite:///" + db_path.as_posix()
        env["VIBEFLOW_STORAGE_BACKEND"] = "fs"

        run_alembic(("upgrade", "head"), env)
        actual = tables(db_path)
        assert EXPECTED <= actual, sorted(EXPECTED - actual)
        assert "alembic_version" in actual
        print(f"1. 空库 upgrade head：{len(EXPECTED)} 张业务表 [OK]")

        repeated = run_alembic(("upgrade", "head"), env)
        assert "Running upgrade" not in repeated.stdout + repeated.stderr
        assert EXPECTED <= tables(db_path)
        print("2. 重复 upgrade head 幂等 [OK]")

        run_alembic(("downgrade", "base"), env)
        assert not (EXPECTED & tables(db_path))
        run_alembic(("upgrade", "head"), env)
        assert EXPECTED <= tables(db_path)
        print("3. downgrade base / 再 upgrade 可逆 [OK]")

    print("\n=== Alembic 临时库迁移冒烟通过 ===")


if __name__ == "__main__":
    main()
