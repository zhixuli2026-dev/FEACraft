"""H14：dictionaryValuesByKey 与父键上下文补全。"""
import json
import urllib.request

B = "http://localhost:8000/api/v1"


def call(path, token):
    request = urllib.request.Request(B + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


request = urllib.request.Request(B + "/auth/login", method="POST",
    data=json.dumps({"email": "dev@vibeflow.cn", "password": "devpass123"}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=30) as response:
    token = json.loads(response.read())["token"]

fixed = call("/completion/openfoam?q=fixed&parent=type&kind=dictionaryValue", token)
labels = {item["label"] for item in fixed["items"]}
assert "fixedValue" in labels and "fixedGradient" in labels, labels
assert all("dictionaryValue" in item["kinds"] and "type" in item["parents"] for item in fixed["items"]), fixed["items"][:5]
print(f"1. parent=type 的值补全 [OK] {sorted(labels)[:6]}")

step = call("/completion/openfoam?q=timeStep&parent=writeControl&kind=dictionaryValue", token)
assert any(item["label"] == "timeStep" for item in step["items"]), step
print("2. parent=writeControl 的值补全 [OK]")

empty = call("/completion/openfoam?q=fixed&parent=not-a-real-parent", token)
assert empty["items"] == [], empty
print("3. 不存在父键返回空而非伪造结果 [OK]")
print("\n=== 父键上下文值补全冒烟通过 ===")
