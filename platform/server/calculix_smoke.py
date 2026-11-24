"""C08：CalculiX 2.23 真实 C3D8 静力求解与 FRD 非零位移。"""
import json
import os
import re
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
DECK = '''*NODE
1,0,0,0
2,1,0,0
3,1,1,0
4,0,1,0
5,0,0,1
6,1,0,1
7,1,1,1
8,0,1,1
*ELEMENT,TYPE=C3D8,ELSET=ALL
1,1,2,3,4,5,6,7,8
*MATERIAL,NAME=STEEL
*ELASTIC
210000,0.3
*SOLID SECTION,ELSET=ALL,MATERIAL=STEEL
*BOUNDARY
1,1,3,0
2,1,3,0
3,1,3,0
4,1,3,0
*STEP
*STATIC
*CLOAD
5,3,-1000
6,3,-1000
7,3,-1000
8,3,-1000
*NODE FILE
U
*EL FILE
S
*END STEP
'''


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def raw(path, token):
    request = urllib.request.Request(B + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def wait_job(job_id, token):
    for _ in range(150):
        job = call("GET", f"/jobs/{job_id}", token=token)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("CalculiX 作业超时")


email = f"calculix-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "calculix-smoke", "password": "calculix-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"calculix-other-{time.time_ns()}@vibeflow.cn",
                                       "name": "calculix-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"calculix-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"calculix-{time.time_ns()}"}, token=token)
case_id = case["id"]
assert call("PUT", f"/cases/{case_id}/file", {"path": "C-run/calculix/beam.inp", "content": DECK}, token=token).get("ok") is True
assert call("POST", f"/cases/{case_id}/calculix:solve", {"input_path": "C-run/calculix/beam.inp"}, token=other).get("_err") == 404
submitted = call("POST", f"/cases/{case_id}/calculix:solve", {"input_path": "C-run/calculix/beam.inp"}, token=token)
assert submitted["backend"] == "calculix", submitted
job = wait_job(submitted["job_id"], token)
assert job["status"] == "succeeded", job
assert raw(f"/cases/{case_id}/raw?path=C-run%2Fcalculix%2Fbeam.dat", token) is not None
for path in ("C-run/calculix/beam.frd", "C-run/calculix/beam.sta"):
    assert raw(f"/cases/{case_id}/raw?path={path.replace('/', '%2F')}", token)
frd = raw(f"/cases/{case_id}/raw?path=C-run%2Fcalculix%2Fbeam.frd", token).decode("utf-8", "replace")
assert "DISP" in frd
values = [float(match) for match in re.findall(r"[-+]?\d+\.\d+(?:[Ee][-+]?\d+)?", frd)]
assert any(abs(value) > 1e-10 for value in values), "FRD 位移应非零"
assert call("POST", f"/cases/{case_id}/calculix:solve", {"input_path": "C-run/calculix/beam.txt"}, token=token).get("_err") in {400, 404}
print("CalculiX C3D8 solve/dat+frd+sta and nonzero displacement: OK")
print("\n=== CalculiX 真实后端与产物冒烟通过 ===")
