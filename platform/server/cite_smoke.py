"""在线文献检索冒烟（Crossref/arXiv 服务端代理；需要出网，断网时标 SKIP 不算失败）"""
import json
import urllib.error
import urllib.request

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data, timeout=20) as r:
        return json.loads(r.read())


tok = call("POST", "/auth/login",
           {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]

try:
    r = call("GET", "/cite/search?q=lid+driven+cavity&source=crossref&limit=3", token=tok)
    assert r["items"] and all(i["bibtex"].startswith("@") and i["key_hint"] for i in r["items"])
    print(f"1. crossref [OK] {len(r['items'])} 条（{r['items'][0]['key_hint']}）")
    r2 = call("GET", "/cite/search?q=cavity+flow&source=arxiv&limit=2", token=tok)
    assert r2["items"] and all("arXiv:" in i["id"] for i in r2["items"])
    print(f"2. arxiv [OK] {len(r2['items'])} 条（{r2['items'][0]['key_hint']}）")
    # 鉴权：无 token 应 401/403
    try:
        call("GET", "/cite/search?q=test&source=crossref")
        raise AssertionError("应拒绝")
    except urllib.error.HTTPError as e:
        assert e.code in (401, 403), e.code
    print("3. 鉴权 [OK]")
    print("\n=== 在线文献检索冒烟通过 ===")
except (urllib.error.URLError, TimeoutError) as e:
    print(f"SKIP：出网不可达（{e}）——在线检索项不纳入本次判定")
