"""Real Edge file-tree and Monaco checks for OpenFOAM dictionary detection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


CASE = "OpenFOAM editor acceptance"
ROOT = "C-run/cavity"
EDIT = ROOT + "/0.15/fluid/alpha.water"
BINARY = ROOT + "/100/phi"
DOTTED_NON_FIELD = ROOT + "/0/ordinary.databytes"
RESTORED = ROOT + "/-0.25/T"


def foam(path, *, cls="volScalarField", form="ascii"):
    object_name = path.split("/")[-1]
    return ("/* OpenFOAM v2406 */\n// physical field\nFoamFile\n{\n"
            f"    version 2.0;\n    format {form};\n    class {cls};\n"
            f"    object {object_name};\n}}\n")


def field(path, cls="volScalarField"):
    value = "(0 0 0)" if "Vector" in cls else "0"
    return foam(path, cls=cls) + f"dimensions [0 0 0 0 0 0 0];\ninternalField uniform {value};\nboundaryField {{}}\n"


TEXT = {
    ROOT + "/0/U": ("openfoam-dict", field(ROOT + "/0/U", "volVectorField")),
    ROOT + "/0.orig/U": ("openfoam-dict", "dimensions [0 1 -1 0 0 0 0];\ninternalField uniform (0 0 0);\n"),
    ROOT + "/0.1/alpha.water": ("openfoam-dict", field(ROOT + "/0.1/alpha.water")),
    ROOT + "/100/U": ("openfoam-dict", field(ROOT + "/100/U", "volVectorField")),
    ROOT + "/processor0/1e-3/air/T": ("openfoam-dict", field(ROOT + "/processor0/1e-3/air/T")),
    EDIT: ("openfoam-dict", field(EDIT)),
    RESTORED: ("openfoam-dict", field(RESTORED)),
    ROOT + "/constant/polyMesh/boundary": ("openfoam-dict", foam("boundary", cls="polyBoundaryMesh") + "0\n(\n)\n"),
    ROOT + "/system/customSolverCoeffs": ("openfoam-dict", foam("customSolverCoeffs", cls="dictionary") + "value 12;\n"),
    ROOT + "/0/check.py": ("python", "print('keep Python in an initial time folder')\n"),
    ROOT + "/0/README.md": ("markdown", "# Initial field inputs\n"),
    ROOT + "/0/notes.txt": ("plaintext", "a known plain text extension\n"),
    "Ref/notes": ("plaintext", "// FoamFile { format ascii; class notAnActualHeader; }\nordinary notes\n"),
}
BINARY_DATA = (foam(BINARY, cls="surfaceScalarField", form="binary").encode()
    + b"internalField nonuniform List<scalar>\n2\n(" + struct.pack("<dd", 1.25, -0.5) + b")\n;\n")
ORDINARY_DATA = b"\x00\x8b\xffordinary payload"


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings["root"])
    base = settings["base"]
    file_url = "/api/v1" + settings["prefix"] + "/file"
    report = {"opened": []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings["channel"])
        context = edge.new_context(viewport={"width": 1450, "height": 940}, locale="zh-CN")
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings["token"]) + ");")
        page = context.new_page()
        page.set_default_timeout(25000)
        expect.set_options(timeout=25000)

        def row(path):
            return page.locator("[data-path=" + json.dumps(path) + "]")

        def tab(path):
            return page.locator("[data-editor-path=" + json.dumps(path) + "]")

        def opened(path):
            parts = path.split("/")
            for index in range(1, len(parts)):
                branch = row("/".join(parts[:index]))
                if branch.get_attribute("aria-expanded") == "false":
                    branch.click()
            with page.expect_response(lambda response: response.request.method == "GET"
                    and urlsplit(response.url).path == file_url) as received:
                row(path).click()
            response = received.value
            assert response.status == 200, response.status
            expect(tab(path)).to_have_attribute("data-active", "true")
            return response.json()

        def editor(language, content):
            page.wait_for_function("""({language, content}) => {
                const model = window.monaco?.editor.getModels().at(-1);
                return model?.getLanguageId() === language && model?.getValue() === content;
            }""", arg={"language": language, "content": content})

        try:
            page.goto(base, wait_until="domcontentloaded")
            page.locator("[data-case-name=" + json.dumps(CASE) + "]").click()
            for path, (language, content) in TEXT.items():
                payload = opened(path)
                assert payload["kind"] == "text" and payload["content"] == content
                assert payload["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
                hinted = language == "openfoam-dict" and path != ROOT + "/0.orig/U"
                assert payload.get("language") == ("openfoam-dict" if hinted else None), path
                editor(language, content)
                if path == ROOT + "/0.1/alpha.water":
                    page.screenshot(path=str(root / "foam-field-editor.png"))
                report["opened"].append({"path": path, "language": language, "api_hint": hinted})

            tab(EDIT).get_by_role("button", name="alpha.water", exact=True).click()
            changed = TEXT[EDIT][1].replace("internalField uniform 0;", "internalField uniform 0.4;")
            page.evaluate("(text) => window.monaco.editor.getModels().at(-1).setValue(text)", changed)
            with page.expect_response(lambda response: response.request.method == "PUT"
                    and urlsplit(response.url).path == file_url) as write:
                page.get_by_role("button", name="保存 (Ctrl+S)", exact=True).click()
            response = write.value
            assert response.status == 200
            assert response.request.post_data_json["expected_sha256"] == hashlib.sha256(TEXT[EDIT][1].encode()).hexdigest()
            expect(tab(EDIT)).to_have_attribute("data-dirty", "false")
            editor("openfoam-dict", changed)
            report["conditional_save_sha256"] = hashlib.sha256(changed.encode()).hexdigest()

            binary = opened(BINARY)
            assert binary["kind"] == "binary" and binary["size"] == len(BINARY_DATA) and "language" not in binary
            panel = page.get_by_test_id("binary-file-download")
            expect(panel).to_be_visible()
            with page.expect_download() as download:
                panel.get_by_role("button", name="下载", exact=True).click()
            item = download.value
            assert item.suggested_filename == "phi" and Path(item.path()).read_bytes() == BINARY_DATA
            binary = opened(DOTTED_NON_FIELD)
            assert binary["kind"] == "binary" and binary["size"] == len(ORDINARY_DATA)
            expect(panel).to_be_visible()
            report["binary_and_ordinary_neighbor"] = True
            report["passed"] = True
        finally:
            (root / "openfoam-browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
            if not report.get("passed"):
                page.screenshot(path=str(root / "openfoam-browser-failure.png"))
            edge.close()


def verify(args):
    import httpx
    from testsupport import IsolatedServer

    api = IsolatedServer("h21-openfoam-editor", app="foam_editor_fixture:app")
    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    (api.root / "foam_editor_fixture.py").write_text("from app.main import app\n"
        "from fastapi.staticfiles import StaticFiles\n"
        + "app.mount('/', StaticFiles(directory=" + repr(str(dist)) + ", html=True))\n", encoding="utf8")
    temp = api.root / "browser-tmp"
    temp.mkdir()
    report = {"passed": False, "model_calls": 0, "mock_llm_calls": 0, "storage": "fs", "database": "sqlite"}
    print("OPENFOAM_EDITOR_EVIDENCE", api.root, flush=True)
    try:
        with api:
            api.create_case(CASE)
            prefix = api.prefix
            case = api.call("GET", prefix)
            query = "/file?" + urlencode({"path": RESTORED})
            api.call("GET", prefix + query, expect=404)
            for path, (_, content) in TEXT.items():
                api.put(path, content)
            with httpx.Client(base_url=api.base + "/api/v1", trust_env=False, timeout=30,
                              headers={"Authorization": "Bearer " + api.token}) as client:
                for path, data in ((BINARY, BINARY_DATA), (DOTTED_NON_FIELD, ORDINARY_DATA)):
                    response = client.post(prefix + "/upload", params={"path": path},
                                           files={"file": (path.rsplit("/", 1)[-1], data, "application/octet-stream")})
                    assert response.status_code == 200, response.status_code
            guest_email = "foam-field-viewer@example.com"
            account = api.call("POST", "/auth/register", {"email": guest_email, "name": "Foam viewer",
                               "password": "isolated-foam-editor-123"})
            guest = {"Authorization": "Bearer " + account["token"]}
            api.call("GET", prefix + query, expect=404, extra_headers=guest)
            org = api.call("GET", "/org/mine")["org"]["id"]
            api.call("POST", f"/org/{org}/members", {"email": guest_email, "role": "viewer"}, expect=201)
            api.call("POST", f"/org/{org}/invitation:accept", {}, extra_headers=guest)
            api.call("PUT", f"/workspaces/{case['workspace_id']}/sharing", {"org_id": org, "role": "viewer"})
            assert api.call("GET", prefix + query, extra_headers=guest)["language"] == "openfoam-dict"
            api.call("PUT", prefix + "/file", {"path": RESTORED, "content": "forbidden"}, expect=404, extra_headers=guest)
            settings = {"root": str(api.root), "base": api.base, "token": api.token, "prefix": prefix, "channel": args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8", str(Path(__file__)), "--browser"],
                input=json.dumps(settings), text=True, check=True, timeout=220, env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            view = json.loads((api.root / "openfoam-browser.json").read_text("utf8"))
            assert len(view["opened"]) == len(TEXT)
            saved = api.call("GET", prefix + "/file?" + urlencode({"path": EDIT}))
            assert saved["language"] == "openfoam-dict" and saved["content_sha256"] == view["conditional_save_sha256"]
            assert api.call("GET", prefix + "/raw?" + urlencode({"path": BINARY}), raw=True) == BINARY_DATA
            report.update(browser=view, previously_missing_file_opened=True, case_acl_checked=True)
        report["passed"] = view["passed"] and api.proc.poll() is not None
    finally:
        report["api_exited"] = api.proc is not None and api.proc.poll() is not None
        (api.root / "openfoam-editor-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    assert report["passed"]
    print("OPENFOAM_EDITOR_PASS", api.root / "openfoam-editor-result.json", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dist", type=Path, required=True)
        parser.add_argument("--browser-python", type=Path, required=True)
        parser.add_argument("--channel", default="msedge")
        verify(parser.parse_args())
