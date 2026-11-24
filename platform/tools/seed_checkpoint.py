"""为 checkpoint E2E 在指定新会话写入 started/checkpoint 并执行恢复。"""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.db import SessionLocal, engine
from app.routers.agent import _append_event, recover_interrupted_runs


async def main():
    cid = int(sys.argv[1])
    run_id = sys.argv[2] if len(sys.argv) > 2 else "checkpoint-ui"
    try:
        async with SessionLocal() as db:
            await _append_event(db, cid, "run_state", {"run_id": run_id, "status": "started"})
            await _append_event(db, cid, "run_checkpoint", {
                "run_id": run_id, "content": "PARTIAL_RECOVERY", "reasoning": "REASONING_RECOVERY"})
            await db.commit()
            assert await recover_interrupted_runs(db, run_id) == 1
    finally:
        await engine.dispose()


asyncio.run(main())
