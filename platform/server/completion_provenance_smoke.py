"""H15：补全来源、生成器/索引 hash、confidence 与确定性排序。"""
import hashlib
from pathlib import Path
import shutil
import tempfile

from app.services import completion
from app.services.completion_builder import scan

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-completion-provenance-"))
try:
    (ROOT / "src" / "demo").mkdir(parents=True)
    (ROOT / "src" / "demo" / "Demo.H").write_text(
        "namespace Demo { class DemoClass {}; }\ntypedef scalar DemoAlias;\n", encoding="utf-8")
    (ROOT / "src" / "demo" / "Demo.C").write_text(
        "addToRunTimeSelectionTable(base, DemoRuntime, dict);", encoding="utf-8")
    (ROOT / "applications" / "solvers" / "demoFoam" / "Make").mkdir(parents=True)
    (ROOT / "applications" / "solvers" / "demoFoam" / "Make" / "files").write_text("demo.C", encoding="utf-8")
    (ROOT / "tutorials" / "demo" / "system").mkdir(parents=True)
    (ROOT / "tutorials" / "demo" / "system" / "controlDict").write_text(
        "application demoFoam;\nwriteControl timeStep;\n", encoding="utf-8")

    first = scan(ROOT, "deterministic-test-source")
    second = scan(ROOT, "deterministic-test-source")
    assert first["generator_sha256"] == hashlib.sha256(
        Path(completion.__file__).with_name("completion_builder.py").read_bytes()).hexdigest()
    assert first["index_sha256"] == second["index_sha256"]
    assert len(first["index_sha256"]) == 64
    assert all(0 < item["confidence"] <= 1 for item in first["completionItems"])
    expected = sorted(first["completionItems"],
                      key=lambda item: (-item["confidence"], -item["occurrences"], item["label"]))
    assert first["completionItems"] == expected
    print("1. 生成器 hash、去时间戳确定性 index hash 与 confidence 排序 [OK]")

    completion._cache = first
    result = completion.search("Demo", limit=20)
    assert result["index_sha256"] == first["index_sha256"]
    assert result["generator_sha256"] == first["generator_sha256"]
    assert result["items"] and all("confidence" in item for item in result["items"])
    assert result["items"] == sorted(result["items"],
        key=lambda item: (-item["confidence"], -item["occurrences"], item["label"]))
    print("2. API 搜索返回 provenance 并使用同一优先级 [OK]")

    (ROOT / "src" / "demo" / "Demo.H").write_text("class ChangedClass {};", encoding="utf-8")
    changed = scan(ROOT, "deterministic-test-source")
    assert changed["index_sha256"] != first["index_sha256"]
    print("3. 输入语义变化会改变 index hash [OK]")
finally:
    completion._cache = None
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n=== 补全 provenance/confidence 冒烟通过 ===")
