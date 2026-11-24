"""F20：Wikipedia provider 适配、结果规范化与真实在线探测（成功才记 LIVE_OK）。"""
import asyncio
import json

from app.services.websearch import CrossrefSearchProvider, WebSearchManager, WikipediaSearchProvider


async def main():
    calls = []
    async def fake_fetch(url):
        calls.append(url)
        return {"ok": True, "result": json.dumps({"query": {"search": [{
            "title": "OpenFOAM", "snippet": "Open-source <span>CFD</span> software"}]}})}
    provider = WikipediaSearchProvider(fetcher=fake_fetch)
    results = await provider.search("OpenFOAM", 1)
    assert results[0]["title"] == "OpenFOAM"
    assert results[0]["snippet"] == "Open-source CFD software"
    assert results[0]["url"].endswith("/OpenFOAM")
    assert "srsearch=OpenFOAM" in calls[0]
    print("1. Wikipedia 响应映射、HTML 清理和 URL 生成 [OK]")

    manager = WebSearchManager(provider=WikipediaSearchProvider())
    event = asyncio.Event()
    result = await manager.execute("live-wikipedia-run", 1, "OpenFOAM", event, max_results=1)
    if result["ok"]:
        assert "provider=wikipedia" in result["result"] and "OpenFOAM" in result["result"], result
        assert "非实时联网 mock" not in result["result"]
        print("2. LIVE_OK Wikipedia 在线返回真实结果")
    else:
        print(f"2. LIVE_LIMITED Wikipedia 在线检索未成功: {result['result']}")

    async def crossref_fake(url):
        return {"ok": True, "result": json.dumps({"message": {"items": [{
            "title": ["OpenFOAM"], "URL": "https://doi.org/10.0000/openfoam",
            "abstract": "CFD <b>research</b>"}]}})}
    crossref = await CrossrefSearchProvider(fetcher=crossref_fake).search("OpenFOAM", 1)
    assert crossref[0]["url"].endswith("/openfoam") and crossref[0]["snippet"] == "CFD research"
    print("3. Crossref 响应映射 [OK]")

    manager = WebSearchManager(provider=CrossrefSearchProvider())
    result = await manager.execute("live-crossref-run", 1, "OpenFOAM", event, max_results=1)
    if result["ok"]:
        assert "provider=crossref" in result["result"] and "OpenFOAM" in result["result"], result
        print("4. LIVE_OK Crossref 在线返回真实结果")
    else:
        print(f"4. LIVE_LIMITED Crossref 在线检索未成功: {result['result']}")


asyncio.run(main())
