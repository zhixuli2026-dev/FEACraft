"""OpenFOAM 补全索引自建器（自有实现；源=官方源码树，不读原版 16.5MB 成品）

输入：OpenFOAM 源码树（src/ applications/ tutorials/）
输出：server/data/completion/openfoam-keywords.json（自有 schema）

采集（全部事实性数据：标识符/频次/来源路径——GPL 源码事实，无表达复制）：
- runtimeTypes：TypeName("X")/defineTypeNameAndDebug(X 宏 + addToRunTimeSelectionTable 注册
- dictionaryKeywords：tutorials 字典文件（controlDict/fvSchemes/fvSolution/blockMeshDict/…）的键频次
- applications：applications/solvers|utilities 的目录名
- headers：src 下公开头文件名（.H）
- completionItems：去重扁平 {label, kinds[], occurrences}
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import time
from pathlib import Path

RE_TYPENAME = re.compile(r'(?:TypeName|ClassName|defineTypeNameAndDebug)\s*\(\s*"?(\w+)"?')
RE_NAMESPACE = re.compile(r'^\s*namespace\s+(\w+)', re.MULTILINE)
RE_CLASS = re.compile(r'^\s*(?:class|struct)\s+(\w+)\s*[{:\n]', re.MULTILINE)
RE_TYPEDEF = re.compile(r'^\s*typedef\s+[\w:<>,\s*&]+?\s+(\w+)\s*;', re.MULTILINE)

RE_ENUM = re.compile(r"^\s*enum(?:\s+class)?\s+(\w+)", re.MULTILINE)
RE_MACRO = re.compile(r"^\s*#\s*define\s+(\w+)", re.MULTILINE)
RE_ENV = re.compile(r"(?:getEnv|getEnvOrDefault|getEnvIfSet)\s*\(\s*\"([A-Z][A-Z0-9_]+)\"")
RE_REGISTRATION = re.compile(
    r"(addToRunTimeSelectionTable(?:ForMethod)?)\s*\(\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)")
RE_CLI = re.compile(r"(?:addBoolOption|addOption|addAlias)\s*\(\s*[\"']([A-Za-z0-9][\w .-]*)[\"']")
RE_FOAM_CLASS = re.compile(r"FoamFile\s*\{[\s\S]*?\bclass\s+([^;]+);")
RE_COMPOUND_KEY = re.compile(r"^\s*(\([\x20-\x7E]+\))\s*(?:\{|[\w\"])", re.MULTILINE)

CPP14_KEYWORDS = """alignas alignof and and_eq asm auto bitand bitor bool break case catch char char16_t
char32_t class compl const const_cast constexpr continue decltype default delete do double dynamic_cast else enum
explicit export extern false final float for friend goto if inline int long mutable namespace new noexcept not
not_eq nullptr operator or or_eq override private protected public register reinterpret_cast return short signed
sizeof static static_assert static_cast struct switch template this thread_local throw true try typedef typeid
typename union unsigned using virtual void volatile wchar_t while xor xor_eq""".split()
CPP_PREPROCESSOR_DIRECTIVES = ["#define", "#elif", "#else", "#endif", "#error", "#if",
                               "#ifdef", "#ifndef", "#include", "#line", "#pragma", "#undef"]
RUNTIME_TAGS = {"boundaryCondition", "combustionModel", "decompositionMethod", "faOption",
                "functionObject", "fvOption", "lagrangianModel", "meshMotion", "sampling",
                "solverComponent", "thermophysicalModel", "turbulenceModel", "utilityComponent"}
RUNTIME_TAG_BASES = {
    "boundaryCondition": {"fvPatch", "polyPatch", "faPatch", "fvPatchField", "patchField"},
    "combustionModel": {"combustionModel", "reactionThermo", "chemistryModel"},
    "decompositionMethod": {"decompositionMethod", "renumberMethod"},
    "faOption": {"faOption"},
    "functionObject": {"functionObject"},
    "fvOption": {"option", "fvOption"},
    "lagrangianModel": {"dragModel", "injectionModel", "liftModel", "wallLubricationModel",
                        "virtualMassModel", "pairPotential", "distributionModel"},
    "meshMotion": {"motionSolver", "dynamicFvMesh", "motionDiffusivity", "solidBodyMotionFunction"},
    "sampling": {"sampledSet", "surfaceWriter", "coordSetWriter", "sampledSurface"},
    "solverComponent": {"ODESolver", "blockEdge", "joint"},
    "thermophysicalModel": {"liquidProperties", "absorptionEmissionModel", "viscosityModel",
                            "thermophysicalFunction", "conductivityModel"},
    "turbulenceModel": {"turbulenceModel", "LESdelta", "turbulentDispersionModel"},
    "utilityComponent": {"topoSetSource", "topoSetCellSource", "topoSetFaceSource",
                         "topoSetPointSource", "searchableSurface", "extrudeModel"},
}

# 证据来源越明确，补全优先级越高；合并多来源时取最高值。
KIND_CONFIDENCE = {
    "runtimeType": 1.0, "application": 1.0, "class": 0.95, "typeAlias": 0.95,
    "cpp14Keyword": 0.95, "enumType": 0.95, "foamFileClass": 0.95,
    "cppPreprocessorDirective": 0.9, "dictionaryKey": 0.9, "compoundDictionaryKey": 0.9,
    "namespace": 0.85, "macro": 0.85, "commandLineToken": 0.85,
    "dictionaryFileName": 0.85, "fieldFileName": 0.85, "wmakeDictionaryKeyword": 0.85,
    "header": 0.8, "environmentVariable": 0.8, "wmakePathVariable": 0.8,
    "dictionaryValue": 0.75,
}

# 字典键：行首 标识符 后跟 值/块/分号
RE_DICT_KEY = re.compile(r'^\s*([A-Za-z_]\w*)\s+(?:[\w"\(\[{;]|[\w.]+;)', re.MULTILINE)
DICT_FILE_NAMES = {
    "controlDict", "fvSchemes", "fvSolution", "blockMeshDict", "decomposeParDict",
    "transportProperties", "turbulenceProperties", "thermophysicalProperties",
    "dynamicMeshDict", "snappyHexMeshDict", "setFieldsDict", "topoSetDict",
    "createPatchDict", "extrudeMeshDict", "surfaceFeatureExtractDict", "mapFieldsDict",
}


def _add_runtime_path_tags(runtime_by_tag: dict[str, set[str]], label: str, path: Path):
    lowered = path.as_posix().lower()
    if "faoptions" in lowered:
        runtime_by_tag["faOption"].add(label)
    if "combustionmodel" in lowered or "combustionmodels" in lowered:
        runtime_by_tag["combustionModel"].add(label)


def _dictionary_text(path: Path) -> str | None:
    limit = 1024 * 1024
    try:
        stream = gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")
        with stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit or any(byte < 32 and byte not in (9, 10, 13) or byte == 127 for byte in raw):
            return None
        text = raw.decode("utf-8-sig")
    except (OSError, EOFError, UnicodeError):
        return None
    text = re.sub(r"//[^\n]*|/\*[\s\S]*?\*/", "", text)
    if re.search(r"\bformat\s+binary\s*;", text):
        return None
    return text


def scan(src_root: Path, source: str = "OpenFOAM-dev version-14（openfoam.org 系，ESI v2406 通道不可达的临时降级）") -> dict:
    t0 = time.time()
    runtime_types: dict[str, int] = {}
    classes: dict[str, int] = {}
    namespaces: dict[str, int] = {}
    typedefs: dict[str, int] = {}
    headers: dict[str, int] = {}
    enum_types: dict[str, int] = {}
    macros: dict[str, int] = {}
    environment_variables: dict[str, int] = {}
    registrations: list[dict] = []
    runtime_by_tag: dict[str, set[str]] = {tag: set() for tag in RUNTIME_TAGS}
    compound_keys: dict[str, int] = {}
    foam_classes: dict[str, int] = {}
    dictionary_files: dict[str, int] = {}
    field_files: dict[str, int] = {}
    command_line_tokens: dict[str, int] = {}
    wmake_paths: dict[str, int] = {}

    src = src_root / "src"
    n_h = 0
    for p in sorted(src.rglob("*.H"), key=lambda item: item.as_posix()):
        if p.is_symlink() or "lnInclude" in p.parts:
            continue
        n_h += 1
        headers[p.name] = headers.get(p.name, 0) + 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in RE_TYPENAME.finditer(text):
            runtime_types[m.group(1)] = runtime_types.get(m.group(1), 0) + 1
            _add_runtime_path_tags(runtime_by_tag, m.group(1), p)
        for m in RE_CLASS.finditer(text):
            classes[m.group(1)] = classes.get(m.group(1), 0) + 1
        for m in RE_NAMESPACE.finditer(text):
            if m.group(1) not in ("Foam",):
                namespaces[m.group(1)] = namespaces.get(m.group(1), 0) + 1
        for m in RE_TYPEDEF.finditer(text):
            typedefs[m.group(1)] = typedefs.get(m.group(1), 0) + 1
        for m in RE_ENUM.finditer(text):
            enum_types[m.group(1)] = enum_types.get(m.group(1), 0) + 1
        for m in RE_MACRO.finditer(text):
            macros[m.group(1)] = macros.get(m.group(1), 0) + 1
        for m in RE_ENV.finditer(text):
            environment_variables[m.group(1)] = environment_variables.get(m.group(1), 0) + 1
    n_c = 0
    for p in sorted(src.rglob("*.C"), key=lambda item: item.as_posix()):
        if p.is_symlink() or "lnInclude" in p.parts:
            continue
        n_c += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in RE_MACRO.finditer(text):
            macros[m.group(1)] = macros.get(m.group(1), 0) + 1
        for m in RE_ENV.finditer(text):
            environment_variables[m.group(1)] = environment_variables.get(m.group(1), 0) + 1
        for m in RE_REGISTRATION.finditer(text):
            macro, base, derived, constructor = m.groups()
            runtime_types[derived] = runtime_types.get(derived, 0) + 1
            registrations.append({"macro": macro, "baseType": base, "derivedType": derived,
                                  "constructor": constructor, "lookupName": derived,
                                  "source": "$WM_PROJECT_DIR/" + p.relative_to(src_root).as_posix()})
            for tag, bases in RUNTIME_TAG_BASES.items():
                if base in bases or tag.lower() == base.lower():
                    runtime_by_tag[tag].add(derived)
            _add_runtime_path_tags(runtime_by_tag, derived, p)

    # 教程字典键频次、父块和按键值
    dict_keys: dict[str, int] = {}
    dict_parents: dict[str, set[str]] = {}
    dict_values: dict[str, set[str]] = {}
    dict_value_counts: dict[tuple[str, str], int] = {}
    dict_value_contexts: dict[tuple[str, str], set[str]] = {}
    dict_value_sources: dict[tuple[str, str], list[str]] = {}
    dict_context_examples: dict[tuple[str, str], dict[str, str]] = {}
    dict_files = 0
    tut = src_root / "tutorials"
    for p in sorted(tut.rglob("*"), key=lambda item: item.as_posix()):
        if p.is_symlink() or not p.is_file():
            continue
        name = p.name.removesuffix(".gz")
        is_dict_file = name in DICT_FILE_NAMES or any(
            part in ("0", "0.orig", "system", "constant") for part in p.parts)
        if not is_dict_file or p.stat().st_size > 1024 * 1024:
            continue
        text = _dictionary_text(p)
        if text is None:
            continue
        dict_files += 1
        if name in DICT_FILE_NAMES or any(part in ("system", "constant") for part in p.parts):
            dictionary_files[name] = dictionary_files.get(name, 0) + 1
        if any(part in ("0", "0.orig") for part in p.parts):
            field_files[name] = field_files.get(name, 0) + 1
        for m in RE_FOAM_CLASS.finditer(text):
            value = m.group(1).strip()
            foam_classes[value] = foam_classes.get(value, 0) + 1
        for m in RE_COMPOUND_KEY.finditer(text):
            value = m.group(1).strip()
            compound_keys[value] = compound_keys.get(value, 0) + 1
        for m in RE_DICT_KEY.finditer(text):
            k = m.group(1)
            if len(k) > 1 and k not in ("FoamFile",):
                dict_keys[k] = dict_keys.get(k, 0) + 1
        stack = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("}"):
                if stack:
                    stack.pop()
                continue
            match = re.match(r"^([A-Za-z_]\w*)\s*(.*)$", line)
            if not match:
                continue
            key, rest = match.groups()
            if rest.startswith("{") or rest.endswith("{"):
                stack.append(key)
                continue
            parent = stack[-1] if stack else ""
            dict_parents.setdefault(key, set()).add(parent)
            value = rest.rstrip(";").strip().strip('"')
            for quoted, bare in re.findall(r'"([^"]+)"|([^\s()]+)', value):
                item = quoted or bare
                if item and len(item) <= 64 and "\ufffd" not in item and not item.startswith(("{", "}", "//")):
                    dict_values.setdefault(key, set()).add(item)
                    pair = (key, item)
                    dict_value_counts[pair] = dict_value_counts.get(pair, 0) + 1
                    dict_value_contexts.setdefault(pair, set()).add(name)
                    source_path = "$WM_PROJECT_DIR/" + p.relative_to(src_root).as_posix()
                    dict_context_examples.setdefault(pair, {}).setdefault(name, source_path)
                    sources = dict_value_sources.setdefault(pair, [])
                    if source_path not in sources and len(sources) < 4:
                        sources.append(source_path)

    # 应用：含 Make/files 的目录即应用/库目录（v14 把 icoFoam 等挪进 legacy/，不按子树猜）
    apps: dict[str, int] = {}
    app_root = src_root / "applications"
    if app_root.is_dir():
        for mk in app_root.rglob("Make/files"):
            name = mk.parent.parent.name
            if name and name not in ("src",):
                apps[name] = apps.get(name, 0) + 1
        for source_file in app_root.rglob("*"):
            if source_file.is_symlink() or "lnInclude" in source_file.parts or not source_file.is_file():
                continue
            if source_file.suffix in (".C", ".H", ".L"):
                text = source_file.read_text(encoding="utf-8", errors="replace")
                for m in RE_CLI.finditer(text):
                    value = "--" + m.group(1)
                    command_line_tokens[value] = command_line_tokens.get(value, 0) + 1
            elif source_file.name == "options" and source_file.parent.name == "Make":
                text = source_file.read_text(encoding="utf-8", errors="replace")
                for value in re.findall(r"\$\((FOAM_[A-Z0-9_]+)\)", text):
                    wmake_paths[value] = wmake_paths.get(value, 0) + 1

    # 汇总扁平 completionItems
    items: dict[str, dict] = {}

    def add(label: str, kind: str, occ: int, parents=(), contexts=(), sources=()):
        if not label or len(label) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in label):
            return
        e = items.setdefault(label, {"label": label, "kinds": set(), "occurrences": 0,
                                     "parents": set(), "confidence": 0.0,
                                     "contexts": set(), "sourceExamples": []})
        e["kinds"].add(kind)
        e["confidence"] = max(e["confidence"], KIND_CONFIDENCE[kind])
        e["occurrences"] += occ
        e["parents"].update(parents)
        e["contexts"].update(contexts)
        for source_path in sources:
            if source_path not in e["sourceExamples"] and len(e["sourceExamples"]) < 4:
                e["sourceExamples"].append(source_path)

    for k, v in runtime_types.items():
        add(k, "runtimeType", v)
    for k, v in classes.items():
        add(k, "class", v)
    for k, v in typedefs.items():
        add(k, "typeAlias", v)     # volScalarField 等（此前漏挂）
    for k, v in namespaces.items():
        add(k, "namespace", v)     # Foam:: 子命名空间（此前漏挂）
    for k, v in dict_keys.items():
        add(k, "dictionaryKey", v, dict_parents.get(k, ()))
    for k, values in sorted(dict_values.items()):
        for value in sorted(values):
            pair = (k, value)
            add(value, "dictionaryValue", dict_value_counts[pair], (k,),
                dict_value_contexts[pair], dict_value_sources[pair])
    for label in CPP14_KEYWORDS:
        add(label, "cpp14Keyword", 1)
    for label in CPP_PREPROCESSOR_DIRECTIVES:
        add(label, "cppPreprocessorDirective", 1)
    for k, v in enum_types.items():
        add(k, "enumType", v)
    for k, v in macros.items():
        add(k, "macro", v)
    for k, v in compound_keys.items():
        add(k, "compoundDictionaryKey", v)
    for k, v in foam_classes.items():
        add(k, "foamFileClass", v)
    for k, v in dictionary_files.items():
        add(k, "dictionaryFileName", v)
    for k, v in field_files.items():
        add(k, "fieldFileName", v)
    for k, v in command_line_tokens.items():
        add(k, "commandLineToken", v)
    for k, v in environment_variables.items():
        add(k, "environmentVariable", v)
    for label in ("EXE", "EXE_INC", "EXE_LIBS", "LIB", "LIB_LIBS"):
        add(label, "wmakeDictionaryKeyword", 1)
    for k, v in wmake_paths.items():
        add(k, "wmakePathVariable", v)
    for k, v in headers.items():
        add(k, "header", v)
    for k in apps:
        add(k, "application", 1)

    out_items = [{"label": e["label"], "kinds": sorted(e["kinds"]),
                  "occurrences": e["occurrences"], "parents": sorted(e["parents"]),
                  "confidence": e["confidence"], "contexts": sorted(e["contexts"]),
                  "sourceExamples": e["sourceExamples"]}
                 for e in items.values()]
    out_items.sort(key=lambda x: (-x["confidence"], -x["occurrences"], x["label"]))

    out = {
        "format": "vibeflow.openfoam-completion-index.own.v2",
        "source": source,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stats": {
            "headers_scanned": n_h, "sources_scanned": n_c,
            "dict_files_scanned": dict_files,
            "runtime_types": len(runtime_types), "classes": len(classes),
            "dictionary_keys": len(dict_keys), "dictionary_parents": len(dict_parents),
            "dictionary_values": len(dict_values), "headers": len(headers),
            "applications": len(apps), "completion_items": len(out_items),
            "macros": len(macros), "enum_types": len(enum_types),
            "registrations": len(registrations), "compound_keys": len(compound_keys),
            "foam_classes": len(foam_classes), "command_line_tokens": len(command_line_tokens),
            "environment_variables": len(environment_variables),
        },
        "languageSyntax": {"cpp14Keywords": CPP14_KEYWORDS,
                           "cppPreprocessorDirectives": CPP_PREPROCESSOR_DIRECTIVES},
        "openfoamCpp": {"enumTypes": [{"keyword": k, "occurrences": v}
                                      for k, v in sorted(enum_types.items())],
                        "macros": [{"keyword": k, "occurrences": v}
                                   for k, v in sorted(macros.items())]},
        "runtimeTypesByTag": {tag: sorted(values) for tag, values in runtime_by_tag.items()},
        "runtimeSelectionRegistrations": registrations,
        "compoundDictionaryKeys": [{"keyword": k, "occurrences": v}
                                    for k, v in sorted(compound_keys.items())],
        "foamFileClasses": [{"keyword": k, "occurrences": v}
                             for k, v in sorted(foam_classes.items())],
        "dictionaryFileNames": [{"fileName": k, "occurrences": v}
                                 for k, v in sorted(dictionary_files.items())],
        "fieldFileNames": [{"fileName": k, "occurrences": v}
                            for k, v in sorted(field_files.items())],
        "commandLineTokens": [{"keyword": k, "occurrences": v}
                               for k, v in sorted(command_line_tokens.items())],
        "environmentAndBuild": {
            "environmentVariables": [{"keyword": k, "occurrences": v}
                                      for k, v in sorted(environment_variables.items())],
            "wmakeDictionaryKeywords": ["EXE", "EXE_INC", "EXE_LIBS", "LIB", "LIB_LIBS"],
            "wmakePathVariables": [{"keyword": k, "occurrences": v}
                                    for k, v in sorted(wmake_paths.items())],
        },
        "dictionaryValuesByKey": [
            {"keyword": key, "values": [
                {"value": value, "occurrences": dict_value_counts[(key, value)],
                 "sourceExamples": dict_value_sources[(key, value)],
                 "contextExamples": dict_context_examples[(key, value)]}
                for value in sorted(values, key=lambda item: (-dict_value_counts[(key, item)], item))
            ]}
            for key, values in sorted(dict_values.items())
        ],
        "dictionaryKeywords": sorted(dict_keys, key=lambda k: -dict_keys[k]),
        "runtimeTypes": sorted(runtime_types, key=lambda k: -runtime_types[k]),
        "applications": sorted(apps),
        "headers": sorted(headers),
        "completionItems": out_items,
    }
    canonical = json.dumps({key: value for key, value in out.items() if key != "generated"},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    out["generator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    out["index_sha256"] = hashlib.sha256(canonical).hexdigest()
    return out


if __name__ == "__main__":
    root = Path(sys.argv[1])
    if not all((root / section).is_dir() for section in ("src", "applications", "tutorials")):
        raise SystemExit("OpenFOAM source requires src, applications and tutorials directories")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else \
        Path(__file__).parent.parent.parent / "data" / "completion" / "openfoam-keywords.json"
    idx = scan(root, sys.argv[3] if len(sys.argv) > 3 else
               "OpenFOAM-dev version-14（openfoam.org 系，ESI v2406 通道不可达的临时降级）")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    print("written:", out_path, idx["stats"])
