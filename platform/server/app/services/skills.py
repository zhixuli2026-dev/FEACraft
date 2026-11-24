"""技能注册表：扫描 server/skills/ 下的 SKILL.md，解析 frontmatter，供 agent 系统提示注入

格式对齐 Claude Code 技能约定（name/description frontmatter + markdown 正文）。
本目录技能为平台自有撰写文本。
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
import json
import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_INTERFACE_FIELDS = ("display_name", "short_description", "default_prompt")


def _openai_interface(skill_dir: Path) -> dict:
    path = skill_dir / "agents" / "openai.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeError):
        return {}
    if text.lstrip().startswith("{"):
        try:
            interface = json.loads(text).get("interface")
        except (AttributeError, json.JSONDecodeError):
            return {}
        if not isinstance(interface, dict):
            return {}
        return {key: interface[key] for key in _INTERFACE_FIELDS if isinstance(interface.get(key), str)}
    interface = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(display_name|short_description|default_prompt)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        interface[key] = value
    return interface


def _parse(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeError):
        return None
    m = _FRONT.match(text)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return {"name": meta.get("name", path.parent.name),
            "description": meta.get("description", ""),
            "body": m.group(2).strip(),
            "path": str(path),
            "interface": _openai_interface(path.parent)}


from .skill_installation import scope as publication_scope
from .skill_store import _identity, skill_directories

_cache: dict[tuple, dict] = {}


def list_skills(user_id: int | None = None, org_id: int | None = None) -> list[dict]:
    """内置 < 组织 < 用户覆盖；不含正文，供列表/提示词目录。"""
    org_key = tuple(org_id) if isinstance(org_id, (list, set)) else org_id
    key = (user_id, org_key)
    directories = skill_directories(user_id, org_id)
    with ExitStack() as held:
        for directory in sorted(set(directories[:-1])):
            held.enter_context(publication_scope(directory))
        return _catalog(key, directories)


def _catalog(key, directories):
    paths = []
    inventory = {}
    for directory in reversed(directories):
        for path in sorted(directory.glob("*/SKILL.md")):
            if path.parent.name.startswith('.'):
                continue
            try:
                inventory[path] = _identity(path.stat())
            except FileNotFoundError:
                continue
            paths.append(path)
            interface = path.parent / "agents" / "openai.yaml"
            try:
                inventory[interface] = _identity(interface.stat())
            except FileNotFoundError:
                pass
    cached = _cache.get(key)
    if cached and inventory == cached["inventory"]:
        return cached["items"]
    merged = {}
    for path in paths:
        parsed = _parse(path)
        if parsed:
            merged[parsed["name"]] = {"name": parsed["name"],
                                      "description": parsed["description"],
                                      **parsed["interface"]}
    items = [merged[name] for name in sorted(merged)]
    _cache[key] = {"inventory": inventory, "items": items}
    return items


def get_skill(name: str, user_id: int | None = None, org_id: int | None = None) -> dict | None:
    if not name or name.startswith('.') or Path(name).name != name or '/' in name or '\\' in name:
        return None
    directories = skill_directories(user_id, org_id)
    builtin = directories[-1]
    for directory in directories:
        guard = nullcontext() if directory == builtin else publication_scope(directory, name=name)
        with guard:
            p = directory / name / "SKILL.md"
            if p.is_file():
                parsed = _parse(p)
                if parsed is not None:
                    return parsed
    return None


def skills_prompt_block(user_id: int | None = None, org_id: int | None = None) -> str:
    """给 agent 系统提示词的技能目录块"""
    skills = list_skills(user_id, org_id)
    if not skills:
        return ""
    lines = ["技能目录（用 get_skill 读取完整指导和支持范围。执行外部工具前，先确认选定运行时具备对应程序、版本与所需许可。）"]
    for s in skills:
        lines.append(f"- ${s['name']}: {s['description']}")
    return "\n".join(lines)


def reload_cache():
    _cache.clear()
