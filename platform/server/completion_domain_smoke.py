"""H14：dictionaryValuesByKey 的文件 context、来源路径和 parent+context 过滤。"""
from pathlib import Path
import shutil
import tempfile

from app.services import completion
from app.services.completion_builder import scan

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-completion-domain-"))
try:
    (ROOT / "tutorials" / "demo" / "system").mkdir(parents=True)
    (ROOT / "tutorials" / "demo" / "system" / "controlDict").write_text(
        "type fixedValue;\nwriteControl timeStep;\n", encoding="utf-8")
    (ROOT / "tutorials" / "demo" / "0").mkdir(parents=True)
    (ROOT / "tutorials" / "demo" / "0" / "U").write_text("type fixedGradient;\n", encoding="utf-8")
    idx = scan(ROOT, "domain-test-source")

    type_values = next(item for item in idx["dictionaryValuesByKey"] if item["keyword"] == "type")
    fixed = next(item for item in type_values["values"] if item["value"] == "fixedValue")
    assert fixed["occurrences"] == 1 and fixed["sourceExamples"] == [
        "$WM_PROJECT_DIR/tutorials/demo/system/controlDict"], fixed
    completion._cache = idx

    field = completion.search("", parent="type", context="U", limit=20)
    assert [item["label"] for item in field["items"]] == ["fixedGradient"], field
    assert field["items"][0]["contexts"] == ["U"], field
    assert field["items"][0]["sourceExamples"] == ["$WM_PROJECT_DIR/tutorials/demo/0/U"], field
    dictionary = completion.search("", parent="type", context="controlDict", limit=20)
    assert [item["label"] for item in dictionary["items"]] == ["fixedValue"], dictionary
    write = completion.search("", parent="writeControl", context="controlDict", limit=20)
    assert [item["label"] for item in write["items"]] == ["timeStep"], write
    assert completion.search("", parent="type", context="missing", limit=20)["items"] == []
    print("parent+file context/value occurrence/sourceExamples: OK")
finally:
    completion._cache = None
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n=== dictionaryValue 文件域上下文冒烟通过 ===")
