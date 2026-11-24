"""H13：原版富分类结构的源码采集与 kind 过滤 golden。"""
from pathlib import Path
import shutil
import tempfile

from app.services import completion
from app.services.completion_builder import scan

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-completion-categories-"))
try:
    (ROOT / "src" / "demo").mkdir(parents=True)
    (ROOT / "src" / "demo" / "Demo.H").write_text(
        "#define DEMO_MACRO 1\nenum class DemoEnum {a};\ntypedef scalar DemoAlias;\n", encoding="utf-8")
    (ROOT / "src" / "demo" / "Demo.C").write_text(
        "addToRunTimeSelectionTable(functionObject, demoFunction, dictionary);\n"
        "const char* value = getEnv(\"DEMO_ENV\");\n", encoding="utf-8")
    (ROOT / "tutorials" / "demo" / "system").mkdir(parents=True)
    (ROOT / "tutorials" / "demo" / "system" / "controlDict").write_text(
        "FoamFile { class dictionary; }\n(left|right) { type fixedValue; }\n", encoding="utf-8")
    (ROOT / "tutorials" / "demo" / "0").mkdir(parents=True)
    (ROOT / "tutorials" / "demo" / "0" / "U").write_text(
        "FoamFile { class volVectorField; }\n", encoding="utf-8")
    (ROOT / "applications" / "solvers" / "demoFoam" / "Make").mkdir(parents=True)
    (ROOT / "applications" / "solvers" / "demoFoam" / "Make" / "files").write_text("demo.C", encoding="utf-8")
    (ROOT / "applications" / "solvers" / "demoFoam" / "Make" / "options").write_text(
        "EXE_INC = $(FOAM_SRC)\n", encoding="utf-8")
    (ROOT / "applications" / "solvers" / "demoFoam" / "demo.C").write_text(
        "argList::addOption(\"demoOption\");\n", encoding="utf-8")

    idx = scan(ROOT, "category-test-source")
    assert "auto" in idx["languageSyntax"]["cpp14Keywords"]
    assert "#define" in idx["languageSyntax"]["cppPreprocessorDirectives"]
    assert idx["openfoamCpp"]["macros"] == [{"keyword": "DEMO_MACRO", "occurrences": 1}]
    assert idx["openfoamCpp"]["enumTypes"] == [{"keyword": "DemoEnum", "occurrences": 1}]
    assert idx["runtimeTypesByTag"]["functionObject"] == ["demoFunction"]
    registration = idx["runtimeSelectionRegistrations"][0]
    assert registration["baseType"] == "functionObject" and registration["derivedType"] == "demoFunction"
    assert registration["constructor"] == "dictionary" and registration["source"].endswith("src/demo/Demo.C")
    assert idx["compoundDictionaryKeys"] == [{"keyword": "(left|right)", "occurrences": 1}]
    assert {item["keyword"] for item in idx["foamFileClasses"]} == {"dictionary", "volVectorField"}
    assert idx["dictionaryFileNames"] == [{"fileName": "controlDict", "occurrences": 1}]
    assert idx["fieldFileNames"] == [{"fileName": "U", "occurrences": 1}]
    assert idx["commandLineTokens"] == [{"keyword": "--demoOption", "occurrences": 1}]
    env = idx["environmentAndBuild"]
    assert env["environmentVariables"] == [{"keyword": "DEMO_ENV", "occurrences": 1}]
    assert env["wmakePathVariables"] == [{"keyword": "FOAM_SRC", "occurrences": 1}]
    print("1. 原版富分类结构与注册来源 [OK]")

    completion._cache = idx
    expected = {
        "cpp14Keyword": "auto", "cppPreprocessorDirective": "#define", "macro": "DEMO_MACRO",
        "enumType": "DemoEnum", "compoundDictionaryKey": "(left|right)",
        "foamFileClass": "volVectorField", "dictionaryFileName": "controlDict",
        "fieldFileName": "U", "commandLineToken": "--demoOption",
        "environmentVariable": "DEMO_ENV", "wmakeDictionaryKeyword": "EXE",
        "wmakePathVariable": "FOAM_SRC",
    }
    for kind, label in expected.items():
        result = completion.search("", kind=kind, limit=100)
        assert any(item["label"] == label for item in result["items"]), (kind, label, result["items"][:10])
    print("2. 12 类 completionItems kind 过滤 [OK]")
finally:
    completion._cache = None
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n=== OpenFOAM 富分类补全冒烟通过 ===")
