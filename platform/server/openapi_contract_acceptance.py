"""Check the served OpenAPI document and actual report response contracts."""
from collections import Counter
import json
from pathlib import Path
import sys

import httpx

from testsupport import IsolatedServer, SERVER

METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
REPORT_FILES = {"/api/v1/cases/{case_id}/report/derivative-artifact", "/api/v1/cases/{case_id}/latex/artifact"}


def check(api, result):
    with httpx.Client(base_url=api.base, timeout=45, trust_env=False) as http:
        docs = http.get("/docs")
        assert docs.is_success and "/openapi.json" in docs.text and "SwaggerUIBundle" in docs.text
        reply = http.get("/openapi.json")
        reply.raise_for_status()
        schema = reply.json()
        operations = [operation for item in schema["paths"].values() for method, operation in item.items()
                      if method in METHODS]
        names = Counter(operation["operationId"] for operation in operations)
        assert names and all(count == 1 for count in names.values()), [name for name, count in names.items() if count > 1]
        result.update(paths=len(schema["paths"]), operations=len(operations), openapi=schema["openapi"])
        spec = schema["components"]["schemas"]
        for route in REPORT_FILES:
            paths = schema["paths"][route]
            assert paths["get"]["operationId"] != paths["head"]["operationId"]
            assert "content" not in paths["head"]["responses"]["200"]
            assert paths["get"]["security"] == [{"HTTPBearer": []}]
            conflict = paths["get"]["responses"]["409"]["content"]["application/json"]["schema"]
            definition = spec[conflict["$ref"].rsplit("/", 1)[-1]]
            nested = definition["properties"]["detail"]["$ref"].rsplit("/", 1)[-1]
            assert set(spec[nested]["required"]) == {"code", "message"}
        (api.root / "openapi.json").write_bytes(reply.content)

        case = api.create_case("OpenAPI report contract")
        headers = {"Authorization": "Bearer " + api.token}
        prefix = f"/api/v1/cases/{case['id']}"
        job = http.post(prefix + "/report:compose", json={"output": "E-report/report.generated.md"}, headers=headers)
        assert job.status_code == 200, (job.status_code, job.text[:500])
        draft = job.json()
        file_api = prefix + "/report/derivative-artifact"
        params = {"publication_id": draft["publication"]["id"], "path": draft["source_path"]}
        actual = http.get(file_api, params=params, headers=headers)
        inspected = http.head(file_api, params=params, headers=headers)
        assert actual.status_code == inspected.status_code == 200 and actual.content, (
            actual.status_code, inspected.status_code, actual.text[:350]
        )
        assert not inspected.content and int(inspected.headers["Content-Length"]) == len(actual.content)
        version = actual.headers["X-Vibeflow-File-Version"]
        assert version == inspected.headers["X-Vibeflow-File-Version"]
        partial = http.get(file_api, params=params, headers={**headers, "Range": "bytes=0-23"})
        assert partial.status_code == 206 and partial.content == actual.content[:24]
        mismatch = "0" * 64 if version != "0" * 64 else "f" * 64
        stale = http.get(file_api, params=params, headers={**headers, "X-Vibeflow-File-Version": mismatch})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "file_changed_during_read"
        assert stale.json()["detail"]["message"]
        assert http.get(file_api, params=params).status_code == 401
        latex = prefix + "/latex/artifact"
        assert http.get(latex, params={"path": "main.pdf"}, headers=headers).status_code == 404
        empty = http.head(latex, params={"path": "main.pdf"}, headers=headers)
        assert empty.status_code == 404 and not empty.content
        result["report"] = {"get": actual.status_code, "head": inspected.status_code,
                            "range": partial.status_code, "bytes": len(actual.content), "stale": stale.json()}
        result["latex"] = {"get": 404, "head": empty.status_code}


def verify():
    api = IsolatedServer("openapi-j26")
    result = {"passed": False, "port": api.port, "model_requests": 0}
    print("OPENAPI_EVIDENCE", api.root, flush=True)
    try:
        with api:
            check(api, result)
        result["api_exit"] = api.proc.poll()
        assert result["api_exit"] is not None
        result["passed"] = True
    except BaseException as error:
        result["failure"] = {"type": type(error).__name__, "message": str(error)[:500]}
        raise
    finally:
        (api.root / "openapi-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    sys.path.insert(0, str(SERVER.parent / "tools"))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    verify()
