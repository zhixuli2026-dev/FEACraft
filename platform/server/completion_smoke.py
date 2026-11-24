"""补全索引冒烟：端点就绪 + 前缀/类型过滤 + 字段正确性"""
import json
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
    with urllib.request.urlopen(req, data) as r:
        return json.loads(r.read())


tok = call("POST", "/auth/login",
           {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]

r = call("GET", "/completion/openfoam?q=write", token=tok)
assert r["ready"], r
labels = [i["label"] for i in r["items"]]
assert "writeControl" in labels and "writeInterval" in labels, labels
assert all(i["label"].startswith("write") for i in r["items"])
print(f"1. 前缀过滤 [OK] q=write → {len(labels)} 项: {labels[:5]}（索引总量 {r['total']}）")

r2 = call("GET", "/completion/openfoam?q=ico", token=tok)
assert any("icoFoam" == i["label"] for i in r2["items"]), [i["label"] for i in r2["items"]]
print("2. 求解器命中 [OK] icoFoam")

r3 = call("GET", "/completion/openfoam?q=GAMG", token=tok)
assert r3["items"], "GAMG 应在字典键/类型里"
print(f"3. 大写前缀 [OK] → {[i['label'] for i in r3['items'][:4]]}")

r4 = call("GET", "/completion/openfoam?q=volScalarField", token=tok)
assert any(i["label"] == "volScalarField" for i in r4["items"])
kinds = next(i["kinds"] for i in r4["items"] if i["label"] == "volScalarField")
print(f"4. 类型别名/类 [OK] volScalarField kinds={kinds}")

r5 = call("GET", "/completion/openfoam?q=&kind=dictionaryKey&limit=10", token=tok)
assert r5["items"] and all("dictionaryKey" in i["kinds"] for i in r5["items"])
print(f"5. kind 过滤 [OK] 前 10: {[i['label'] for i in r5['items'][:5]]}")

print("\n=== 补全索引冒烟通过 ===")
