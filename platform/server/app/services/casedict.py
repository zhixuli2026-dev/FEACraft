"""caseDict 服务：OpenFOAM 字典语法解析 + StandardCase 脚手架

契约来源：逆向提取的 StandardCase（workflowState A/B/C/D/E/V、目录布局、分区名）。
注意：脚手架文本为自有撰写，只保留机制兼容（不复制原版 15.6KB 注释模板原文）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .casedict_schema import error_counts, validate_case_dict_data

CASE_DICT_PATH = "0-caseDict/caseDict"
INTENT_LIST_FIELDS = {"targetOutputs", "acceptanceCriteria", "assumptions", "limitations",
                      "openQuestions", "decisionLog", "primaryAssumptions", "knownLimitations"}

# ---------------- OpenFOAM 字典解析（精简但真实可用） ----------------

_TOKEN = re.compile(r"""
    (?P<comment>//[^\n]*|/\*.*?\*/)      # 注释
  | (?P<brace>[{}])                       # 大括号
  | (?P<string>"(?:[^"\\]|\\.)*")         # 字符串
  | (?P<word>[A-Za-z0-9_.$+*/:|<>\[\](),=-]+)  # 裸词（含单位/路径等字符）
  | (?P<semi>;)
  | (?P<ws>\s+)
""", re.VERBOSE | re.DOTALL)


@dataclass
class DictEntry:
    key: str
    value: object = None          # str | list[str] | SubDict
    children: dict = field(default_factory=dict)  # 当 value 为子字典时


def tokenize(text: str):
    pos = 0
    for m in _TOKEN.finditer(text):
        if m.start() != pos:
            raise ValueError(f"caseDict 解析失败：位置 {pos} 附近的字符无法识别 {text[pos:pos+30]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("comment", "ws"):
            continue
        yield kind, m.group()
    if pos != len(text):
        raise ValueError(f"caseDict 解析失败：位置 {pos} 起无法识别")


def _string_value(value: str) -> str:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # OpenFOAM also permits escapes outside JSON (for example regex paths).
        return value[1:-1]


def _parenthesized_items(values: list, first_kind: str, last_kind: str) -> list | None:
    if (not values or first_kind != "word" or last_kind != "word"
            or not values[0].startswith("(") or not values[-1].endswith(")")):
        return None
    if len(values) == 1:
        item = values[0][1:-1]
        return [item] if item else []
    first, last = values[0][1:], values[-1][:-1]
    return ([first] if first else []) + values[1:-1] + ([last] if last else [])


def parse_casedict(text: str) -> dict:
    """把 OpenFOAM 字典文本解析成嵌套 dict：{key: value | {…}}；重复 key 收敛为 list"""
    tokens = list(tokenize(text))
    i = 0

    def parse_block(until_brace: bool, *, block_path: tuple[str, ...] = ()) -> dict:
        nonlocal i
        out: dict = {}
        while i < len(tokens):
            kind, val = tokens[i]
            if kind == "brace" and val == "}":
                if not until_brace:
                    raise ValueError("多余的 }")
                i += 1
                return out
            if kind != "word" and kind != "string":
                raise ValueError(f"期望关键字，得到 {val!r}")
            key = _string_value(val) if kind == "string" else val
            if key == "workflowState" and key in out:
                raise ValueError("workflowState 不得重复定义")
            i += 1
            # 子字典
            if i < len(tokens) and tokens[i] == ("brace", "{"):
                i += 1
                out[key] = parse_block(True, block_path=(*block_path, key))
                if i < len(tokens) and tokens[i][1] == ";":
                    i += 1  # 块后可选分号
                continue
            # 标量/列表值：读到分号
            values = []
            value_start = i
            while i < len(tokens) and tokens[i][0] != "semi":
                k2, v2 = tokens[i]
                if k2 == "brace":
                    raise ValueError(f"键 {key!r} 的值中出现意外的 {v2!r}")
                values.append(_string_value(v2) if k2 == "string" else v2)
                i += 1
            if i >= len(tokens):
                raise ValueError(f"键 {key!r} 缺少结尾分号")
            list_field = ((block_path == ("caseMeta",) and key == "tags")
                          or (block_path == ("intent",) and key in INTENT_LIST_FIELDS)
                          or (block_path == ("numericalMethod", "decomposition") and key == "divisions")
                          or (len(block_path) == 3 and block_path[:2] == ("errors", "entries")
                              and key == "relatedPaths")
                          or (block_path[:2] == ("extensions", "entries")
                              and block_path[3:4] == ("settings",)))
            items = (_parenthesized_items(values, tokens[value_start][0], tokens[i - 1][0])
                     if list_field and values else None)
            if items is None and block_path == ("caseMeta",) and key == "tags" and len(values) == 1:
                items = values
            out[key] = items if items is not None else (values[0] if len(values) == 1 else values)
            i += 1
        if until_brace:
            raise ValueError("子块缺少结束花括号 }")
        return out

    result = parse_block(False)
    return result


class LiteralDictString(str):
    """A supplied string is data, including any surrounding quote characters."""


UNCHANGED_DICT_ENTRY = object()


def _scalar_text(value) -> str:
    if isinstance(value, LiteralDictString):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    # Legacy form/adapter callers may supply one already quoted OpenFOAM token.
    # Preserve that token, but quote arbitrary embedded quotes as literal text.
    if re.fullmatch(r'"(?:[^"\\]|\\.)*"', text):
        return text
    if text and re.fullmatch(r"[A-Za-z0-9_.$+*/:|<>\[\](),=-]+", text) and not any(
            marker in text for marker in ("//", "/*")):
        return text
    return json.dumps(text, ensure_ascii=False)


def write_casedict(d: dict, indent: int = 0) -> str:
    """序列化回 OpenFOAM 字典文本"""
    pad = "    " * indent
    lines = []
    for k, v in d.items():
        key = _scalar_text(k)
        if isinstance(v, dict):
            lines.append(f"{pad}{key}\n{pad}{{")
            lines.append(write_casedict(v, indent + 1))
            lines.append(f"{pad}}}")
        elif isinstance(v, list):
            lines.append(f"{pad}{key} ({' '.join(_scalar_text(x) for x in v)});")
        else:
            sv = _scalar_text(v)
            lines.append(f"{pad}{key} {sv};")
    return "\n".join(lines)


# ---------------- 块级定位与替换（表单编辑器用；保注释/保其它块） ----------------
def find_block_span(text: str, name: str) -> tuple[int, int] | None:
    """顶级块 `name { ... }` 的**内部内容**文本区间（花括号之间）。找不到返回 None。"""
    entry = next((entry for entry in reversed(_dict_entry_spans(text)) if entry.key == name), None)
    return (entry.value_start + 1, entry.value_end - 1) if entry and entry.is_block else None


@dataclass(frozen=True)
class _EntrySpan:
    key: str
    start: int
    key_end: int
    value_start: int
    value_end: int
    end: int
    is_block: bool


def _dict_entry_spans(text: str) -> list[_EntrySpan]:
    """定位当前层的键、值和可选分号，单独保留注释及空白的位置。"""
    toks = [(m.lastgroup, m.group(), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    spans = []
    i = 0
    while i < len(toks):
        kind, val, st, en = toks[i]
        if kind in ("ws", "comment"):
            i += 1
            continue
        if kind not in ("word", "string"):
            i += 1
            continue
        key = _string_value(val) if kind == "string" else val
        j = i + 1
        while j < len(toks) and toks[j][0] in ("ws", "comment"):
            j += 1
        value_start = toks[j][2] if j < len(toks) else en
        value_end = value_start
        has_semi = False
        is_block = j < len(toks) and toks[j][:2] == ("brace", "{")
        if is_block:
            depth = 0
            while j < len(toks):
                k, v, _s, e = toks[j]
                if k == "brace":
                    depth += 1 if v == "{" else -1
                    if depth == 0:
                        value_end = e
                        break
                j += 1
            else:
                raise ValueError(f"键 {key!r} 的子块缺少结束花括号")
            j += 1
            while j < len(toks) and toks[j][0] in ("ws", "comment"):
                j += 1
            if j < len(toks) and toks[j][0] == "semi":
                has_semi = True
        else:
            while j < len(toks):
                k, v, _s, e = toks[j]
                if k == "semi":
                    has_semi = True
                    break
                if k == "brace":
                    raise ValueError(f"键 {key!r} 的值中出现意外的 {v!r}")
                if k not in ("ws", "comment"):
                    value_end = e
                j += 1
            else:
                raise ValueError(f"键 {key!r} 缺少结尾分号")
        spans.append(_EntrySpan(key, st, en, value_start, value_end,
                                toks[j][3] if has_semi else value_end, is_block))
        i = j + 1 if has_semi else j
    return spans


def _entry_value(value) -> str:
    rendered = write_casedict({"_": value})[1:].lstrip()
    return rendered[:-1] if rendered.endswith(";") else rendered


def _apply_edits(text: str, edits) -> str:
    parts = []
    previous = 0
    for start, end, value in sorted(edits, key=lambda edit: edit[0]):
        parts.extend((text[previous:start], value))
        previous = end
    parts.append(text[previous:])
    return "".join(parts)


def _value_tokens(text: str, entry: _EntrySpan):
    return [m for m in _TOKEN.finditer(text, entry.value_start, entry.value_end)
            if m.lastgroup not in ("ws", "comment")]


def _drop_entry(text: str, entry: _EntrySpan):
    edits = [(entry.start, entry.key_end, "")]
    if entry.is_block:
        edits.append((entry.value_start, entry.value_end, ""))
    else:
        edits.extend((token.start(), token.end(), "") for token in _value_tokens(text, entry))
    if entry.end > entry.value_end:
        edits.append((entry.end - 1, entry.end, ""))
    return edits


def _value_edits(text: str, entry: _EntrySpan, value, newline: str):
    start, end = entry.value_start, entry.value_end
    if entry.is_block and isinstance(value, dict):
        updated = _edit_block(text[start + 1:end - 1], value, newline)
        return [(start + 1, end - 1, updated)]
    rendered = _entry_value(value)
    if entry.is_block or isinstance(value, dict):
        if not isinstance(value, dict) and entry.end == end:
            rendered += ";"
        if start == end:
            rendered = " " + rendered
        return [(start, end, rendered.replace("\n", newline))]

    old = _value_tokens(text, entry)
    new = list(tokenize(rendered))
    old_values = [_string_value(m.group()) if m.lastgroup == "string" else m.group() for m in old]
    new_values = [_string_value(token) if kind == "string" else token for kind, token in new]
    # The generic parser exposes some lists as OpenFOAM atoms. The form
    # echoes those arrays verbatim when another field is edited.
    if old_values == new_values or (isinstance(value, list) and value == old_values):
        return []
    if isinstance(value, list) and old and new:
        items = _parenthesized_items(old_values, old[0].lastgroup, old[-1].lastgroup)
        if items is not None and items == _parenthesized_items(new_values, new[0][0], new[-1][0]):
            return []
    changes = []
    for index, match in enumerate(old):
        if index >= len(new_values):
            changes.append((match.start(), match.end(), ""))
            continue
        changed = old_values[index] != new_values[index]
        # Quotes can be adjacent to bare tokens. Keep a separator if either
        # neighbor changes so the resulting words cannot merge.
        separate = (index > 0 and old[index - 1].end() == match.start()
                    and (changed or old_values[index - 1] != new_values[index - 1]))
        if changed or separate:
            token = new[index][1] if changed else match.group()
            changes.append((match.start(), match.end(), " " + token if separate else token))
    if len(new) > len(old):
        changes.append((end, end, " " + " ".join(token for _, token in new[len(old):])))
    return changes


def _edit_block(original: str, entries: dict, newline: str, *, replace_missing: bool = True) -> str:
    spans = _dict_entry_spans(original)
    last = {entry.key: entry for entry in spans}
    additions = {key: value for key, value in entries.items() if key not in last}
    if additions and not replace_missing:
        raise ValueError(f"键 {next(iter(additions))!r} 不存在于块内")
    edits = []
    for entry in spans:
        if entry.key not in entries:
            if replace_missing:
                edits.extend(_drop_entry(original, entry))
        elif last[entry.key] is entry and entries[entry.key] is not UNCHANGED_DICT_ENTRY:
            edits.extend(_value_edits(original, entry, entries[entry.key], newline))
    if additions:
        last_line = original.rfind("\n") + 1
        tail = original[last_line:]
        before_closer = last_line > 0 and not tail.strip()
        position = last_line if before_closer else len(original)
        prefix = "" if before_closer else newline
        indent = tail + "    " if before_closer else "    "
        if spans:
            first = spans[0].start
            existing_indent = original[original.rfind("\n", 0, first) + 1:first]
            if existing_indent and not existing_indent.strip():
                indent = existing_indent
        added = newline.join(indent + line for line in write_casedict(additions).split("\n"))
        edits.append((position, position, prefix + added + newline))
    return _apply_edits(original, edits)


def set_block(text: str, name: str, entries: dict, replace_missing: bool = True) -> str:
    """Edit only the named top-level block, keeping surrounding source text."""
    span = find_block_span(text, name)
    newline = "\r\n" if "\r\n" in text else "\n"
    if span is None:
        if not replace_missing:
            raise ValueError(f"块 {name!r} 不存在")
        appended = write_casedict({name: entries}).replace("\n", newline)
        spacer = newline if text.endswith("\n") else newline * 2
        return text + spacer + appended + newline
    start, end = span
    updated = _edit_block(text[start:end], entries, newline, replace_missing=replace_missing)
    return text[:start] + updated + text[end:]


# 桌面版宽容解析对齐：workflowState B; / workflowState {B}; / 小写 / dev(=V) / 可省略分号
_RE_STATE = re.compile(
    r"\bworkflowState\s*(?:\{\s*([A-Ea-evV]|dev)\s*\}|([A-Ea-evV]|dev)\b)\s*;?")
_RE_STATE_WRITE = re.compile(
    r"(^\s*workflowState\s+(?:\{\s*)?)(?:[A-Ea-evV]|dev)(\s*\}?\s*;?)", re.MULTILINE)


def _norm_state(s: str) -> str:
    s = s.strip()
    return "V" if s.lower() == "dev" else s.upper()


def get_workflow_state(text: str) -> str:
    tokens = list(tokenize(text))
    depth = 0
    for i, (kind, value) in enumerate(tokens):
        if depth == 0 and kind in ("word", "string") and value.strip('"') == "workflowState":
            rest = tokens[i + 1:]
            if rest and rest[0] == ("brace", "{"):
                rest = rest[1:]
            if rest and rest[0][0] == "word" and re.fullmatch(r"[A-Ea-evV]|dev", rest[0][1]):
                return _norm_state(rest[0][1])
            return "A"
        if kind == "brace":
            depth += 1 if value == "{" else -1
    return "A"


def set_workflow_state(text: str, state: str) -> str:
    if state not in ("A", "B", "C", "D", "E", "V"):
        raise ValueError("非法目标阶段")
    for entry in _dict_entry_spans(text):
        if entry.key == "workflowState":
            newline = "\r\n" if "\r\n" in text else "\n"
            text = _apply_edits(text, _value_edits(text, entry, state, newline))
            break
    else:
        text = f"workflowState {state};\n" + text
    meta = parse_casedict(text).get("caseMeta", {})
    if isinstance(meta, dict) and "status" in meta and state != "V":
        statuses = {"A": "readyForGeometry", "B": "readyForMesh", "C": "readyForRun",
                    "D": "readyForPost", "E": "readyForReport"}
        text = set_block(text, "caseMeta", {"status": statuses[state]}, replace_missing=False)
    return text


class WorkflowGateError(ValueError):
    """阶段确认、版本或工程证据未满足；不得写入任何内容。"""


@dataclass(frozen=True)
class WorkflowTransition:
    """仅由完成证据检查的专用推进接口创建，不从普通写入请求反序列化。"""
    previous_state: str
    target_state: str
    expected_revision: int
    confirmed: bool

    def validate(self, case, target: str):
        if not self.confirmed:
            raise WorkflowGateError("阶段推进必须由用户显式确认")
        if (self.expected_revision != case.dict_revision or
                self.previous_state != case.workflow_state or self.target_state != target):
            raise WorkflowGateError("caseDict 已变化，请刷新后重新确认阶段")
        order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
        if target not in ("A", "V") and (
                case.workflow_state not in order or target not in order or
                order[target] != order[case.workflow_state] + 1):
            raise WorkflowGateError("阶段不可跳级；V 开发态须先回到 A")


def validate_mesh_transition(case, parsed, target):
    from .mesh_presets import effective_targets
    mesh = parsed.get("mesh", {})
    targets = effective_targets(mesh)
    policy = targets.get("policy") if isinstance(targets, dict) else None
    if target == "C" and case.workflow_state in ("A", "B") and policy in ("fail", "allowWithReason"):
        from .mesh_quality import mesh_gate_reason
        reason = mesh_gate_reason(case.s3_prefix, mesh, policy, targets.get("reason"))
        if reason:
            raise WorkflowGateError(reason)


def _validate_update(case, content: str, source: str = "api", *,
                     allow_failure_regression: bool = False,
                     transition: WorkflowTransition | None = None,
                     expected_revision: int | None = None, check_mesh: bool = True) -> dict:
    """校验格式、阶段与工程门禁，不修改文件和数据库镜像。"""
    previous_state = case.workflow_state
    if expected_revision is not None and expected_revision != case.dict_revision:
        raise WorkflowGateError("caseDict 已被其他操作修改，请刷新后重试")
    parsed = parse_casedict(content)
    schema_errors = validate_case_dict_data(parsed)
    if schema_errors:
        raise ValueError("; ".join(f"{e['path']}: {e['message']}" for e in schema_errors))
    next_state = _norm_state(parsed["workflowState"])
    if next_state != previous_state:
        if allow_failure_regression:
            order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "V": 5}
            if source != "job-failure-fallback" or order[next_state] >= order[previous_state]:
                raise WorkflowGateError("失败回退只能回到更早阶段")
        elif transition is None:
            raise WorkflowGateError("普通保存和 Agent 提案不得修改 workflowState；请在算例面板确认阶段推进")
        else:
            transition.validate(case, next_state)
    if next_state != previous_state and not allow_failure_regression:
        counts = error_counts(parsed)
        if counts["open"] or counts["blocking"]:
            raise WorkflowGateError(
                f"存在 {counts['open']} 个未结清错误和 {counts['blocking']} 个阻断错误，禁止推进 workflowState")
        if check_mesh:
            validate_mesh_transition(case, parsed, next_state)
    return parsed


@dataclass(frozen=True)
class PreparedCaseDict:
    parsed: dict
    revision: int
    workflow_state: str
    case_meta: str
    files: dict[str, bytes]
    expected_files: dict


def prepare_case_dict(case, content, source='api', *, extra_events=(), existing_files=None, actor_id=None, **options):
    """Validate and render all three files before touching any live bytes."""
    from types import SimpleNamespace
    import hashlib
    from .. import storage
    from .case_events import CASE_EVENTS_PATH, build_case_event, prepare_case_events
    from .provenance import PROVENANCE_PATH, prepare_case_revision
    parsed = _validate_update(case, content, source, **options)
    existing = ({path: storage.get_bytes(case.s3_prefix, path) if storage.exists(case.s3_prefix, path) else None
                 for path in (CASE_EVENTS_PATH, PROVENANCE_PATH)} if existing_files is None
                else {path: existing_files.get(path) for path in (CASE_EVENTS_PATH, PROVENANCE_PATH)})
    expected = {path: None if data is None else {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for path, data in existing.items()}
    meta = parsed.get('caseMeta', {})
    case_meta = json.dumps(meta, ensure_ascii=False) if isinstance(meta, dict) else '{}'
    draft = SimpleNamespace(id=getattr(case, 'id', None), s3_prefix=case.s3_prefix,
                            workflow_state=get_workflow_state(content), dict_revision=case.dict_revision + 1)
    events = [build_case_event(draft, 'caseDict.updated', source, case.workflow_state)]
    events.extend(build_case_event(draft, event_type, event_source, previous, details)
                  for event_type, event_source, previous, details in extra_events)
    data = content.encode('utf8')
    _, provenance = prepare_case_revision(draft, parsed, source, case.workflow_state, data,
                                         existing=existing[PROVENANCE_PATH] or b'', actor_id=actor_id)
    return PreparedCaseDict(parsed, draft.dict_revision, draft.workflow_state, case_meta,
                            {CASE_DICT_PATH: data, CASE_EVENTS_PATH: prepare_case_events(draft, events,
                                existing=existing[CASE_EVENTS_PATH] or b''), PROVENANCE_PATH: provenance}, expected)


def _apply_dict_mirror(case, prepared):
    case.workflow_state = prepared.workflow_state
    case.dict_revision = prepared.revision
    case.parse_status = "ok"
    case.case_meta = prepared.case_meta


def update_case_dict(case, content: str, source: str = 'api', **options) -> dict:
    """Synchronous constructor/import helper; live writes use write_case_dict."""
    from .. import storage
    prepared = prepare_case_dict(case, content, source, **options)
    for path, data in prepared.files.items():
        storage.put_bytes(case.s3_prefix, path, data)
    _apply_dict_mirror(case, prepared)
    return prepared.parsed


async def write_case_dict(db, case, content: str, source: str = 'api', *, expected_files=None, **options) -> dict:
    """Caller holds the case coordinator and owns commit; all bytes are journaled."""
    from .file_transactions import write
    prepared = prepare_case_dict(case, content, source, **options)
    conditions = {**(expected_files or {}), **prepared.expected_files}
    async with db.begin_nested():
        await write(db, case, prepared.files, expected_files=conditions,
                    content_types={path: 'text/plain; charset=utf-8' for path in prepared.files})
        _apply_dict_mirror(case, prepared)
        await db.flush()
    return prepared.parsed


# ---------------- StandardCase 脚手架（自有文本，契约兼容） ----------------

CASE_DICT_TEMPLATE = """// VibeFlow unified case source of truth.
// Solver-neutral problem description; workflow stages:
// A:geometry; B:mesh; C:run; D:post; E:report; V:development.
workflowState A;

caseMeta
{
    // caseName        ;
    // discipline      ;   // e.g. fluid / solid / thermal
    // analysisType    ;   // e.g. steady / transient
    // executionProfile;   // local | container | remote | HPC
}

intent
{
    // userIntent        ;
    // targetOutputs     ;
    // acceptanceCriteria;
}

physicalProblem
{
    // problemClass ;  // e.g. incompressibleFlow
    // timeBehavior ;  // steadyState | transient
}

geometry
{
    // generator ;   // cadquery | gmsh | external | manual
    // parameters { }
}

materials
{
    // entries { water { role fluid; density 998.2; } }
}

regions
{
}

interfaces
{
}

boundariesLoadsInitials
{
    boundarySets
    {
        // inlet { role inlet; patch inlet; physicalType velocity; value (1 0 0); unit m/s; confirmed false; }
    }
    initialConditions
    {
        // initialization uniform; fields { U (0 0 0); }
    }
    loadCases
    {
        // nominal { activeBoundaries (inlet); activePhysics (fluid); }
    }
    // activeLoadCase nominal;
}

mesh
{
    // source blockMesh; qualityTargets { maxNonOrtho 70; }
}

numericalMethod
{
    // formulation FVM; timeControl { }
}

execution
{
    // runMode local;
}

postProcessing
{
    // objectives ( );
}

report
{
    // title ""; language zh-CN; outputFile "E-report/report.md";
    // template auto; // auto uses caseMeta.discipline and caseMeta.analysisType
    // referenceDoc "templates/brand.docx"; css ("styles/report.css");
}

provenance
{
    createdBy "";
    createdAt "";
    updatedAt "";
    sources { }
    assumptions { }
    decisions { }
    artifactTrace { }
}

errors
{
    // entries { }
}

extensions
{
}
"""

SCAFFOLD_FILES: dict[str, str] = {
    "0-caseDict/caseDict": CASE_DICT_TEMPLATE,
    "0-caseDict/memory.md": "",
    "A-geo/geometry.py": (
        '"""参数化几何入口。契约：实现 build_geometry(ctx) / export_geometry(ctx, objects)"""\n'
        "def build_geometry(ctx):\n"
        "    # TODO: 用 CadQuery 建模并 return 对象列表\n"
        "    return []\n"
        "\n"
        "def export_geometry(ctx, objects):\n"
        "    pass\n"
    ),
    "B-mesh/.gitkeep": "",
    "C-run/.gitkeep": "",
    "D-post/postprocess.py": (
        '"""后处理脚本：numpy/matplotlib 数据分析与出版级出图"""\n'
    ),
    "D-post/scenes.py": (
        '"""场景脚本：平台监听本文件变更并自动执行到当前可视化会话。\n'
        "约定：对象命名 VF_* 前缀，增量语义（同名更新参数）。\n"
        "可用函数见技能 vibeflow-viz（VF_surface/VF_slice/VF_contour/VF_stream/VF_glyph/\n"
        'VF_color/VF_remove/VF_time/VF_reset_camera/VF_shot/VF_load）。\n'
        "日志：D-post/logs/viz_executor.log；截图输出到 D-post/figures/。\n"
        '"""\n'
    ),
    "D-post/pvpython.py": '"""离线批处理脚本（pvpython 执行）"""\n',
    "D-post/summary.md": "",
    "E-report/report.md": "",
    "UDF/README.md": "",
    "Ref/.gitkeep": "",
}

# 目录用途说明（给前端/文档用）
STAGE_DIRS = {
    "0-caseDict": "需求与参数源",
    "A-geo": "A: 参数化几何",
    "B-mesh": "B: 网格",
    "C-run": "C: 求解",
    "D-post": "D: 后处理",
    "E-report": "E: 报告",
    "UDF": "V: 开发模式",
    "Ref": "参考资料",
}
