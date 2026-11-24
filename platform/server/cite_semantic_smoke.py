"""Semantic Scholar 适配器、429/Retry-After 退避和可区分的在线验证。"""
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

import httpx
from fastapi import HTTPException

from app.routers.cite import _search_semantic_scholar

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


async def unit_tests():
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": str(len(calls))}, request=request)
        return httpx.Response(200, json={"data": [{
            "paperId": "abc123", "title": "OpenFOAM verification", "year": 2026,
            "authors": [{"name": "Jane Doe"}], "externalIds": {"DOI": "10.1000/demo"},
        }]}, request=request)

    async def fake_sleep(delay):
        delays.append(delay)

    items = await _search_semantic_scholar("OpenFOAM", 1, httpx.MockTransport(handler), fake_sleep)
    assert len(calls) == 3 and delays == [1.0, 2.0], (len(calls), delays)
    assert items[0]["source"] == "semanticscholar" and items[0]["id"] == "doi:10.1000/demo", items
    assert items[0]["year"] == "2026" and "Jane Doe" in items[0]["bibtex"], items[0]
    assert "doi = {10.1000/demo}" in items[0]["bibtex"], items[0]["bibtex"]
    print("1. 两次 429 后成功、Retry-After=[1,2]、字段/BibTeX 规范化 [OK]")

    rate_calls = []

    def always_rate_limited(request):
        rate_calls.append(request)
        return httpx.Response(429, request=request)

    delays.clear()
    try:
        await _search_semantic_scholar("OpenFOAM", 1, httpx.MockTransport(always_rate_limited), fake_sleep)
        raise AssertionError("持续 429 应返回 503")
    except HTTPException as error:
        assert error.status_code == 503, error
    assert len(rate_calls) == 3 and delays == [1.0, 2.0], (len(rate_calls), delays)
    print("2. 持续 429 最多三次请求、指数退避、最终 503 [OK]")

    def network_error(request):
        raise httpx.ConnectError("offline", request=request)

    try:
        await _search_semantic_scholar("OpenFOAM", 1, httpx.MockTransport(network_error), fake_sleep)
        raise AssertionError("网络错误应返回 502")
    except HTTPException as error:
        assert error.status_code == 502, error
    print("3. 网络失败明确返回 502 [OK]")


asyncio.run(unit_tests())
token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
query = urllib.parse.urlencode({"q": "OpenFOAM", "source": "semanticscholar", "limit": 1})
live = call("GET", f"/cite/search?{query}", token=token)
if live.get("items"):
    item = live["items"][0]
    assert item["source"] == "semanticscholar" and item["title"] and item["bibtex"], item
    print(f"4. LIVE_OK Semantic Scholar 在线返回 {len(live['items'])} 条 [OK]")
elif live.get("_err") in (502, 503):
    print(f"4. LIVE_LIMITED 公共 API 当前返回 {live['_err']}；仅 mock 适配/退避通过，不记在线成功")
else:
    raise AssertionError(f"在线 API 非预期响应: {live}")
unauthorized = call("GET", f"/cite/search?{query}")
assert unauthorized.get("_err") in (401, 403), unauthorized
print("5. 未认证请求拒绝 [OK]")
print("\n=== Semantic Scholar 适配器冒烟通过（在线结果见 LIVE_ 标记）===")
