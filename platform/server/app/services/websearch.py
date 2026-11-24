"""可取消的 web_search provider adapter，默认使用 Crossref。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import re
from typing import Protocol
from urllib.parse import quote

from .webfetch import cancellable_web_fetch


def _json_result(result: dict):
    text = result["result"]
    if text.startswith("[web_fetch]"):
        text = text.split("\n", 1)[1]
    return json.loads(text)


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> list[dict]: ...


class MockSearchProvider:
    name = "mock"

    def __init__(self, delay: float = 0.05):
        self.delay = delay

    async def search(self, query: str, max_results: int) -> list[dict]:
        await asyncio.sleep(self.delay)
        return [{
            "title": f"{query} — 确定性 mock 结果 {index + 1}",
            "url": f"https://example.invalid/search/{index + 1}?q={quote(query)}",
            "snippet": "这是用于验证 web_search 链路的本地 mock 内容，不是实时网页结果。",
        } for index in range(max_results)]


class WikipediaSearchProvider:
    name = "wikipedia"

    def __init__(self, timeout_seconds: float = 15, fetcher=None):
        self.timeout_seconds = timeout_seconds
        self.fetcher = fetcher

    async def search(self, query: str, max_results: int) -> list[dict]:
        url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&utf8=1"
               f"&srlimit={max_results}&srsearch={quote(query)}")
        cancel_event = asyncio.Event()
        if self.fetcher is not None:
            result = await self.fetcher(url)
        else:
            result = await cancellable_web_fetch(url, cancel_event, self.timeout_seconds)
        if not result.get("ok"):
            raise RuntimeError(result.get("result", "Wikipedia 检索失败"))
        payload = _json_result(result)
        records = payload.get("query", {}).get("search", [])
        if not isinstance(records, list):
            raise ValueError("Wikipedia 响应缺少 query.search")
        return [{"title": item.get("title", ""),
                 "url": f"https://en.wikipedia.org/wiki/{quote(item.get('title', '').replace(' ', '_'))}",
                 "snippet": re.sub(r"<[^>]+>", "", item.get("snippet", ""))}
                for item in records[:max_results]]


class CrossrefSearchProvider:
    name = "crossref"

    def __init__(self, timeout_seconds: float = 15, fetcher=None):
        self.timeout_seconds = timeout_seconds
        self.fetcher = fetcher

    async def search(self, query: str, max_results: int) -> list[dict]:
        url = f"https://api.crossref.org/works?query={quote(query)}&rows={max_results}"
        cancel_event = asyncio.Event()
        if self.fetcher is not None:
            result = await self.fetcher(url)
        else:
            result = await cancellable_web_fetch(url, cancel_event, self.timeout_seconds)
        if not result.get("ok"):
            raise RuntimeError(result.get("result", "Crossref 检索失败"))
        payload = _json_result(result)
        records = payload.get("message", {}).get("items", [])
        if not isinstance(records, list):
            raise ValueError("Crossref 响应缺少 message.items")
        output = []
        for item in records[:max_results]:
            title = (item.get("title") or [""])[0]
            snippet = item.get("abstract") or "; ".join(item.get("container-title") or [])
            output.append({"title": title,
                           "url": item.get("URL") or f"https://doi.org/{item.get('DOI', '')}",
                           "snippet": re.sub(r"<[^>]+>", "", snippet)})
        return output


@dataclass
class SearchRun:
    run_id: str
    conversation_id: int
    remaining: int
    queries: int = 0
    status: str = "queued"


class WebSearchManager:
    """每个 Agent run 一个 runId；同一 run 内多次搜索共享有界预算。"""

    def __init__(self, provider: SearchProvider | None = None, budget: int = 4):
        self.provider = provider or MockSearchProvider()
        self.budget = budget
        self._runs: dict[str, SearchRun] = {}

    def snapshot(self, run_id: str) -> dict | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return {"run_id": run.run_id, "conversation_id": run.conversation_id,
                "remaining": run.remaining, "queries": run.queries, "status": run.status}

    def finish(self, run_id: str):
        self._runs.pop(run_id, None)

    async def execute(self, run_id: str, conversation_id: int, query: str,
                      cancel_event: asyncio.Event, max_results: int = 5) -> dict:
        query = query.strip()
        if not query or len(query) > 500:
            return {"ok": False, "status": "error", "run_id": run_id,
                    "result": "query 必须为 1–500 字符"}
        max_results = max(1, min(max_results, 10))
        run = self._runs.get(run_id)
        if run is None:
            run = SearchRun(run_id=run_id, conversation_id=conversation_id, remaining=self.budget)
            self._runs[run_id] = run
        if run.conversation_id != conversation_id:
            return {"ok": False, "status": "error", "run_id": run_id,
                    "result": "runId 与会话不匹配，已拒绝"}
        if run.remaining <= 0:
            run.status = "error"
            return {"ok": False, "status": "error", "run_id": run_id,
                    "result": f"本回合共享搜索预算 {self.budget} 次已用尽"}

        run.remaining -= 1
        run.queries += 1
        run.status = "running"
        provider_task = asyncio.create_task(self.provider.search(query, max_results))
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({provider_task, cancel_wait},
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done:
                provider_task.cancel()
                try:
                    await provider_task
                except asyncio.CancelledError:
                    pass
                run.status = "cancelled"
                return {"ok": False, "status": "cancelled", "run_id": run_id,
                        "result": f"web_search 已取消（runId={run_id}）"}
            try:
                results = provider_task.result()
            except Exception as error:
                run.status = "error"
                return {"ok": False, "status": "error", "run_id": run_id,
                        "result": f"web_search provider 失败: {type(error).__name__}: {error}"}
            lines = [f"[provider={self.provider.name}] 查询：{query}（非实时联网 mock）"
                     if self.provider.name == "mock" else
                     f"[provider={self.provider.name}] 查询：{query}"]
            for index, item in enumerate(results[:max_results], 1):
                lines.append(f"{index}. {item.get('title', '')}\n{item.get('url', '')}\n{item.get('snippet', '')}")
            run.status = "completed"
            return {"ok": True, "status": "completed", "run_id": run_id,
                    "result": "\n".join(lines)}
        finally:
            provider_task.cancel()
            cancel_wait.cancel()
            await asyncio.gather(provider_task, cancel_wait, return_exceptions=True)


def _default_provider() -> SearchProvider:
    provider = os.getenv("VIBEFLOW_WEB_SEARCH_PROVIDER", "").strip().lower() or "crossref"
    if provider == "wikipedia":
        return WikipediaSearchProvider()
    if provider == "crossref":
        return CrossrefSearchProvider()
    if provider == "mock":
        return MockSearchProvider()
    raise ValueError("不支持的 VIBEFLOW_WEB_SEARCH_PROVIDER")


web_search_manager = WebSearchManager(provider=_default_provider())
