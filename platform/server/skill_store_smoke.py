"""F61：用户/组织技能目录、覆盖、打包安装与恶意 ZIP 拒绝。"""
import asyncio
import io
import json
import time
import urllib.error
import urllib.request
import zipfile

from app.db import SessionLocal, engine
from app.models import Membership

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def download(path, token):
    request = urllib.request.Request(B + path, data=b"", method="POST",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def register(prefix):
    return call("POST", "/auth/register", {"email": f"{prefix}-{time.time_ns()}@vibeflow.cn",
                                           "name": prefix, "password": "skill-store-pass-123"})


async def share(org_id, user_id):
    async with SessionLocal() as db:
        db.add(Membership(org_id=org_id, user_id=user_id, role="member"))
        await db.commit()
    await engine.dispose()


def install(package, token):
    boundary = f"----vibeflow{time.time_ns()}"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"skill.zip\"\r\n"
            "Content-Type: application/zip\r\n\r\n").encode() + package + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(f"{B}/skills/install", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return urllib.request.urlopen(request, timeout=30)


a = register("skill-owner")
b = register("skill-other")
ta, tb = a["token"], b["token"]
org_id = call("GET", "/org/mine", token=ta)["org"]["id"]
name = f"custom-{time.time_ns()}"
created = call("POST", "/skills", {"name": name, "description": "custom skill", "body": "USER_BODY",
    "display_name": "Custom Skill", "short_description": "short", "default_prompt": f"Use ${name}"}, token=ta)
assert created.get("ok"), created
assert name in {skill["name"] for skill in call("GET", "/skills", token=ta)["skills"]}
assert name not in {skill["name"] for skill in call("GET", "/skills", token=tb)["skills"]}
package = download(f"/skills/{name}/package?scope=user", ta)
with zipfile.ZipFile(io.BytesIO(package)) as archive:
    assert f"{name}/SKILL.md" in archive.namelist()
    assert f"{name}/agents/openai.yaml" in archive.namelist()
print("1. 用户创建/隔离/ZIP 打包 [OK]")

org_name = f"org-{time.time_ns()}"
org_created = call("POST", "/skills", {"name": org_name, "description": "org skill", "body": "ORG_BODY",
                                       "scope": "org"}, token=ta)
assert org_created.get("ok"), org_created
asyncio.run(share(org_id, b["user"]["id"]))
assert org_name in {skill["name"] for skill in call("GET", "/skills", token=tb)["skills"]}
user_override = call("POST", "/skills", {"name": org_name, "description": "user override", "body": "USER_OVERRIDE",
                                         "scope": "user"}, token=ta)
assert user_override.get("ok"), user_override
assert call("GET", f"/skills/{org_name}", token=ta)["body"] == "USER_OVERRIDE"
assert call("GET", f"/skills/{org_name}", token=tb)["body"] == "ORG_BODY"
print("2. 组织共享且用户覆盖组织 [OK]")

package = download(f"/skills/{org_name}/package?scope=user", ta)
with install(package, tb) as response:
    installed = json.loads(response.read())
assert installed["name"] == org_name, installed
assert call("GET", f"/skills/{org_name}", token=tb)["body"] == "USER_OVERRIDE"
try:
    install(package, tb)
    raise AssertionError("重复安装应 409")
except urllib.error.HTTPError as error:
    assert error.code == 409, error.code
print("3. ZIP 安装与默认不覆盖 [OK]")

malicious = io.BytesIO()
with zipfile.ZipFile(malicious, "w") as archive:
    archive.writestr("../evil/SKILL.md", "---\nname: evil\ndescription: evil\n---\nbody")
try:
    install(malicious.getvalue(), tb)
    raise AssertionError("恶意 ZIP 应拒绝")
except urllib.error.HTTPError as error:
    assert error.code == 400, error.code
print("4. 路径穿越 ZIP fail closed [OK]")
print("\n=== 用户/组织技能存储/打包/安装冒烟通过 ===")
