"""F13：Codex 容量定向续跑、退避上限、错误分类与停止等待。"""
import asyncio

from app.services.codex_capacity import CAPACITY_DELAYS, run_with_capacity_retry


async def main():
    delays = []
    original_calls = 0
    continue_calls = 0

    async def record_sleep(delay):
        delays.append(delay)

    async def original():
        nonlocal original_calls
        original_calls += 1
        raise RuntimeError("Selected model is at capacity")

    async def continued():
        nonlocal continue_calls
        continue_calls += 1
        if continue_calls == 1:
            raise RuntimeError('{"code":"serverOverloaded"}')
        return "resumed"

    result = await run_with_capacity_retry(
        original, continued, sleep=record_sleep, jitter=lambda _: 0)
    assert result == "resumed" and original_calls == 1 and continue_calls == 2
    assert delays == [5.0, 10.0], delays
    print("1. 文本/原生容量错误只续跑原线程，原请求不重复 [OK]")

    for message in ("quota exhausted", "login failed", "context length exceeded",
                    "network connection failed", "turn completion timed out"):
        calls = 0

        async def fatal(message=message):
            nonlocal calls
            calls += 1
            raise RuntimeError(message)

        async def must_not_continue():
            raise AssertionError("非容量错误不得续跑")

        try:
            await run_with_capacity_retry(
                fatal, must_not_continue, sleep=record_sleep, jitter=lambda _: 0)
            raise AssertionError("非容量错误应直接抛出")
        except RuntimeError as error:
            assert str(error) == message
        assert calls == 1
    print("2. 额度/登录/上下文/网络/回合超时均不盲重试 [OK]")

    delays.clear()
    calls = 0

    async def always_capacity():
        nonlocal calls
        calls += 1
        raise RuntimeError("serverOverloaded")

    try:
        await run_with_capacity_retry(
            always_capacity, always_capacity, sleep=record_sleep, jitter=lambda _: 0)
        raise AssertionError("持续容量错误应在五次续跑后失败")
    except RuntimeError as error:
        assert "serverOverloaded" in str(error)
    assert calls == 6 and delays == list(CAPACITY_DELAYS), (calls, delays)
    print("3. 5/10/20/40/60 秒退避与五次续跑上限 [OK]")

    cancel = asyncio.Event()
    waiting = asyncio.Event()
    continued_after_cancel = False

    async def blocked_sleep(_delay):
        waiting.set()
        await asyncio.Event().wait()

    async def continue_after_cancel():
        nonlocal continued_after_cancel
        continued_after_cancel = True
        return "wrong"

    task = asyncio.create_task(run_with_capacity_retry(
        original, continue_after_cancel, cancel=cancel,
        sleep=blocked_sleep, jitter=lambda _: 0))
    await asyncio.wait_for(waiting.wait(), 1)
    cancel.set()
    try:
        await asyncio.wait_for(task, 1)
        raise AssertionError("停止应取消容量等待")
    except asyncio.CancelledError:
        pass
    assert not continued_after_cancel
    print("4. 停止取消退避等待，不再发送继续请求 [OK]")
    print("\n=== Codex 容量定向重试冒烟通过 ===")


asyncio.run(main())
