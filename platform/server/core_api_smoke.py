"""Real HTTP regression against an isolated disposable SQLite/fs API."""
from testsupport import IsolatedServer


def main():
    with IsolatedServer("core-api") as api:
        api.create_case("core-integrity")
        original = api.call("GET", api.prefix + "/caseDict")
        for target in ("B", "C", "D", "E", "V"):
            api.call("PUT", api.prefix + "/caseDict", {
                "content": original["content"].replace("workflowState A;", f"workflowState {target};")},
                expect=409)
        api.call("PUT", api.prefix + "/caseDict", {
            "content": original["content"] + "\nunfinished { x 1;"}, expect=422)
        api.call("PUT", api.prefix + "/caseDict", {
            "content": original["content"], "workflow_state": "AB"}, expect=422)
        current = api.call("GET", api.prefix + "/caseDict")
        assert current["content"] == original["content"]
        assert current["dict_revision"] == original["dict_revision"]
        api.advance("B", expect=409)
        api.advance("E", expect=409)
        api.advance("V", confirmed=False, expect=409)
        for target in ("", "AB", "ABC", "X"):
            api.advance(target, expect=422)
        api.advance("V")
        api.advance("D", expect=409)
        api.advance("A")
        api.call("PUT", api.prefix + "/caseDict", {
            "content": original["content"], "expected_revision": original["dict_revision"]}, expect=409)
        for value, source, target, expected in ((1000, "mm", "m", 1), (0, "degC", "K", 273.15)):
            result = api.call("POST", api.prefix + "/caseDict/units:convert", {
                "dimension": "temperature" if source == "degC" else "length", "value": value,
                "source": source, "target": target})
            assert abs(result["value"] - expected) < 1e-12
        # Input files can disappear/change between API preflight and process start.
        # An external malformed file must leave a failed Job, never a queued orphan.
        for path in ("system/controlDict", "constant/polyMesh/points", "constant/polyMesh/faces",
                     "constant/polyMesh/owner", "constant/polyMesh/boundary"):
            api.put("C-run/start-failure/" + path, "fixture")
        case_dict = next((api.root / "files").rglob("0-caseDict/caseDict"))
        saved = case_dict.read_bytes()
        try:
            case_dict.write_text("unfinished {", encoding="utf-8")
            api.call("POST", api.prefix + "/jobs", {"type": "solve", "command": "echo should-not-start",
                     "run_dir": "C-run/start-failure"}, expect=409)
            jobs = api.call("GET", api.prefix + "/jobs")
            assert jobs[0]["status"] == "failed" and jobs[0]["exit_code"] == -1
            assert "should-not-start" not in jobs[0].get("log_backlog", "")
        finally:
            case_dict.write_bytes(saved)
        print("CORE_API_OK: bypass/truncation/revision/confirmation/V-state/units; developer data untouched")


if __name__ == "__main__":
    main()
