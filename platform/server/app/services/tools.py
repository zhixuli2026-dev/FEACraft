"""Agent 内置工具集：定义（OpenAI/Anthropic 双格式）+ 沙箱执行器

文件工具限定在当前算例工作区内；审批后的 shell 选择该目录作为 cwd，不提供硬文件隔离：
  read_text_file / list_files / search_text / web_search / web_fetch / run_shell_command /
  propose_file_update / propose_text_replacement / update_plan / request_approval

危险工具（需审批）：run_shell_command、propose_file_update（apply 时）
对应桌面版 agentBridge 的 webApproveTool/webApplyProposal 语义。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from ..storage import _fs_path  # 工作区路径解析（含防穿越）
from .async_ops import finish_before_cancel
from .case_write import is_case_dictionary


# ---------------- 工具定义 ----------------
# (name, description, params_schema, dangerous)
SHELL_ENVIRONMENT = ("当前执行宿主是 Windows，命令可选 cmd、PowerShell 或 WSL bash。" if os.name == 'nt' else
                     "当前执行宿主是 Linux，默认 Shell 是 /bin/sh；请使用 POSIX 命令，不要使用 cmd、PowerShell 或 wsl.exe。")
TOOLS: list[tuple[str, str, dict, bool]] = [
    ("read_text_file", "读取工作区内的文本文件内容", {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对算例根目录的路径"}},
        "required": ["path"],
    }, False),
    ("list_files", "列出算例工作区的文件（可按子目录过滤；默认不含 .vibeflow 内部作业日志）", {
        "type": "object",
        "properties": {"subdir": {"type": "string", "description": "子目录，如 D-post"},
                       "include_internal": {"type": "boolean", "default": False,
                                            "description": "是否包含 .vibeflow/ 内部目录"}},
    }, False),
    ("search_text", "在算例文件里全文搜索关键字，返回 文件:行号:内容", {
        "type": "object",
        "properties": {"query": {"type": "string"}, "subdir": {"type": "string"}},
        "required": ["query"],
    }, False),
    ("web_search", "通过配置的搜索服务查询网页或文献，返回标题、链接与摘要", {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "1–500 字符搜索词"},
                       "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10}},
        "required": ["query"],
    }, False),
    ("web_fetch", "安全读取公开网页（DNS/IP/重定向校验、TLS 验证、8 MiB 上限）", {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "绝对 http/https URL，不含凭据"}},
        "required": ["url"],
    }, False),
    ("run_shell_command", "在算例工作区执行命令。" + SHELL_ENVIRONMENT, {
        "type": "object",
        "properties": {"command": {"type": "string"},
                       "timeout_seconds": {"type": "integer", "default": 120},
                       **({"runtime": {"type": "string", "enum": ["windows", "wsl", "powershell"], "default": "windows"}} if os.name == 'nt' else {})},
        "required": ["command"],
    }, True),  # 危险：审批
    ("propose_file_update", "提议整文件覆写（生成 diff 提案，用户批准后生效）", {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                       "reason": {"type": "string"}},
        "required": ["path", "content", "reason"],
    }, True),  # 危险：审批
    ("propose_text_replacement", "提议精确文本替换（聚焦编辑首选；用户批准后生效）", {
        "type": "object",
        "properties": {"path": {"type": "string"},
                       "old_text": {"type": "string", "description": "要替换的现有精确文本"},
                       "new_text": {"type": "string"},
                       "reason": {"type": "string"},
                       "replace_all": {"type": "boolean", "default": False}},
        "required": ["path", "old_text", "new_text", "reason"],
    }, True),  # 危险：审批
    ("update_plan", "更新任务计划（待办清单）", {
        "type": "object",
        "properties": {"steps": {"type": "array", "items": {"type": "object",
            "properties": {"title": {"type": "string"},
                           "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
            "required": ["title", "status"]}}},
        "required": ["steps"],
    }, False),
    ("update_goal", "更新当前会话目标（仅持久化目标/验收/预算/状态，不自动续轮）", {
        "type": "object",
        "properties": {"objective": {"type": "string"},
                       "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                       "budget": {"type": "object", "properties": {
                           "max_turns": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                           "max_tool_calls": {"type": "integer", "minimum": 1, "maximum": 500, "default": 40}}},
                       "status": {"type": "string", "enum": ["active", "completed", "cancelled", "blocked"], "default": "active"},
                       "stop_reason": {"type": "string"}},
        "required": ["objective"],
    }, False),
    ("update_memory", "追加或压缩当前算例的长期经验记忆（仅 0-caseDict/memory.md，64 KiB 有界保留）", {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["append", "compact"], "default": "append"},
                       "content": {"type": "string", "description": "append 时必填的经验文本，单次最多 16 KiB"}},
    }, False),
    ("get_skill", "读取指定技能的完整指导文本（当任务命中某技能的领域时调用）", {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "技能名（不含 $ 前缀）"}},
        "required": ["name"],
    }, False),
]

DANGEROUS_TOOLS = {name for name, _, _, d in TOOLS if d}
PLAN_BLOCKED_TOOLS = DANGEROUS_TOOLS | {"update_memory"}

def tools_for_openai() -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
            for n, d, p, _ in TOOLS]


def tools_for_anthropic() -> list[dict]:
    return [{"name": n, "description": d, "input_schema": p} for n, d, p, _ in TOOLS]


PLAN_RESULT_PREFIX = "计划已更新:\n"


def plan_payload(steps: list[dict]) -> list[dict]:
    if not isinstance(steps, list):
        raise ValueError("steps 必须是步骤列表")
    result = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("title"), str) or not step["title"].strip():
            raise ValueError("每个计划步骤都须提供标题")
        status = step.get("status")
        if status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"非法计划状态: {status}")
        result.append({"title": step["title"], "status": status})
    if sum(step["status"] == "in_progress" for step in result) > 1:
        raise ValueError("最多只能有一个 in_progress 步骤")
    return result


def goal_payload(args: dict) -> dict:
    objective = args.get("objective")
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 1000:
        raise ValueError("objective 必须为 1–1000 字符")
    criteria = args.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or len(criteria) > 10 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 300 for item in criteria):
        raise ValueError("acceptance_criteria 最多 10 条，每条 1–300 字符")
    budget = args.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("budget 必须是对象")
    max_turns = budget.get("max_turns", 10)
    max_tool_calls = budget.get("max_tool_calls", 40)
    if type(max_turns) is not int or not 1 <= max_turns <= 100:
        raise ValueError("max_turns 必须为 1–100")
    if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 500:
        raise ValueError("max_tool_calls 必须为 1–500")
    status = args.get("status", "active")
    if status not in ("active", "completed", "cancelled", "blocked"):
        raise ValueError("非法目标状态")
    stop_reason = args.get("stop_reason", "")
    if not isinstance(stop_reason, str) or len(stop_reason) > 500:
        raise ValueError("stop_reason 最多 500 字符")
    stop_reason = stop_reason.strip()
    if status != "active" and not stop_reason:
        raise ValueError("目标终态必须提供 stop_reason")
    if status == "active" and stop_reason:
        raise ValueError("active 目标不应设置 stop_reason")
    return {"objective": objective.strip(),
            "acceptance_criteria": [item.strip() for item in criteria],
            "budget": {"max_turns": max_turns, "max_tool_calls": max_tool_calls},
            "status": status, "stop_reason": stop_reason}


PROPOSAL_TOOLS = frozenset({'propose_file_update', 'propose_text_replacement'})
STAGED_FILE_TOOLS = PROPOSAL_TOOLS | {'update_memory'}


def proposal_digest(args):
    from .case_write import CaseWriteConflict

    if "baseline_sha256" not in args:
        raise CaseWriteConflict("file_conflict", "旧提案缺少文件基线，请重新读取文件并提出修改", path=args["path"])
    return args["baseline_sha256"]


def replacement_arguments(args):
    old, new, replace_all = args.get('old_text'), args.get('new_text'), args.get('replace_all', False)
    if not isinstance(old, str) or not old or not isinstance(new, str) or type(replace_all) is not bool:
        raise ValueError('文本替换需要非空原文、替换文本和布尔型 replace_all')
    if '\x00' in old or '\x00' in new:
        raise ValueError('文本替换参数不能包含空字节')
    return old, new, replace_all


def proposal_edit(name, args, current):
    path = args['path']
    if name == 'propose_file_update':
        return args['content'], {'result': f'文件已写入: {path}'}
    old, new, replace_all = replacement_arguments(args)
    count = current.count(old)
    if count == 0:
        raise ValueError('old_text 在文件中不存在（文件可能已被改动，需重新提案）')
    if count > 1 and not replace_all:
        raise ValueError(f'old_text 出现 {count} 次，非唯一（需 replace_all）')
    content = current.replace(old, new) if replace_all else current.replace(old, new, 1)
    result = f"{path} 已更新（{'全部' if replace_all else '单处'}替换 {count if replace_all else 1} 处）"
    return content, {'result': result}


# ---------------- 执行器 ----------------
class ToolExecutor:
    """限定在指定算例的 S3 前缀（本地 fs）内执行工具"""

    def __init__(self, s3_prefix: str,
                 case_dict_writer: Callable[[str], None] | None = None,
                 user_id: int | None = None, org_id: int | None = None,
                 case_id: int | None = None):
        self.prefix = s3_prefix
        self._case_dict_writer = case_dict_writer
        self.user_id = user_id
        self.org_id = org_id
        self.case_id = case_id

    def _path(self, rel: str) -> Path:
        return _fs_path(self.prefix, rel)

    def _write_text(self, path: str, content: str):
        from .attempt_workspace import assert_mutable_path
        assert_mutable_path(self.prefix, path)
        norm = PurePosixPath(path.replace("\\", "/").lstrip("/")).as_posix()
        if is_case_dictionary(self.prefix, norm):
            if self._case_dict_writer is None:
                raise ValueError("caseDict 写入必须携带 DB 镜像同步回调")
            self._case_dict_writer(content)
            return
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if p.exists():
                os.chmod(temporary, p.stat().st_mode)
            os.replace(temporary, p)
            from .case_write import record_write
            record_write(self.prefix, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def proposal_baseline(self, name: str, args: dict, snapshot=False) -> dict:
        return self._bound_proposal(name, args, snapshot=snapshot)

    def prepare_file_proposal(self, name: str, args: dict):
        from .agent_file_reads import ReplacementScan

        scan = None
        if name == 'propose_text_replacement':
            old, new, replace_all = replacement_arguments(args)
            scan = ReplacementScan(old.encode('utf8'), new.encode('utf8'), replace_all)
        prepared = self._bound_proposal(name, args, snapshot=True, replacement=scan)
        return prepared, None if scan is None else scan.error

    def _bound_proposal(self, name, args, *, snapshot, replacement=None):
        if name not in PROPOSAL_TOOLS:
            return dict(args)
        # The current storage backend is authoritative; an S3 file need not
        # have a local mirror. Approval rechecks this hash under the case lock.
        from .agent_file_reads import proposal_source
        digest, previous = proposal_source(self.prefix, args.get('path'), snapshot=snapshot, replacement=replacement)
        prepared = {key: value for key, value in args.items() if key != 'proposal_snapshot'}
        prepared['baseline_sha256'] = digest
        if previous is not None:
            from .conversation_import import MAX_IMPORT_LINE_BYTES

            args_size = len(json.dumps(prepared, ensure_ascii=False).encode('utf8'))
            preview_size = len(json.dumps(previous, ensure_ascii=False).encode('utf8'))
            if previous['kind'] == 'text' and args_size + preview_size + 8192 > MAX_IMPORT_LINE_BYTES:
                previous = {'kind': 'limit'}
            prepared['proposal_snapshot'] = previous
        return prepared

    def is_dictionary_proposal(self, name, args):
        return name in PROPOSAL_TOOLS and is_case_dictionary(self.prefix, args['path'])

    def _assert_baseline(self, path: str, expected_sha256: str | None):
        current = self._path(path)
        digest = hashlib.sha256(current.read_bytes()).hexdigest() if current.is_file() else None
        if digest != expected_sha256:
            raise ValueError("文件在提案后发生外部修改，请重新读取并生成新提案")

    def execute(self, name: str, args: dict) -> dict:
        """返回 {"ok": bool, "result": str} —— 结果会作为 tool 消息回给模型"""
        fn = getattr(self, f"_tool_{name}", None)
        if fn is None:
            return {"ok": False, "result": f"未知工具: {name}"}
        try:
            return {"ok": True, "result": fn(**args)}
        except Exception as e:
            return {"ok": False, "result": f"{type(e).__name__}: {e}"}

    # ---- 文件类 ----
    def _tool_read_text_file(self, path: str) -> str:
        from .agent_file_reads import read_text
        return read_text(self.prefix, path)

    def _tool_list_files(self, subdir: str = "", include_internal: bool = False) -> str:
        from .agent_file_reads import list_files
        return list_files(self.prefix, subdir, include_internal)

    def _tool_search_text(self, query: str, subdir: str = "") -> str:
        from .agent_file_reads import search_text
        return search_text(self.prefix, query, subdir)

    # ---- 命令执行（危险，审批后才走到这里）----
    def _tool_run_shell_command(self, command: str, timeout_seconds: int = 120,
                                runtime: str = "windows") -> str:
        raise ValueError('Agent shell commands require execute_case_tool with a claimed, owned tool call')

    # ---- 提案类（写操作走审批，这里只登记）----
    def _tool_propose_file_update(self, path: str, content: str, reason: str) -> str:
        return f"已登记文件覆写提案: {path}（{len(content)} 字符）。理由: {reason}。等待用户批准。"

    def _tool_propose_text_replacement(self, path: str, old_text: str, new_text: str,
                                       reason: str, replace_all: bool = False) -> str:
        # 登记前预检：old_text 必须在现文件中存在（且唯一，除非 replace_all）
        p = self._path(path)
        if not p.is_file():
            return f"文件不存在: {path}"
        current = p.read_text(encoding="utf-8", errors="replace")
        n = current.count(old_text)
        if n == 0:
            return f"[预检失败] 在 {path} 中找不到要替换的文本——先用 read_text_file 核对现状"
        if n > 1 and not replace_all:
            return f"[预检失败] old_text 在 {path} 中出现 {n} 次——加长文本保证唯一，或 replace_all=true"
        return (f"已登记文本替换提案: {path}（{'全部' if replace_all else '单处'}替换，"
                f"{len(old_text)}→{len(new_text)} 字符）。理由: {reason}。等待用户批准。")

    def _tool_update_plan(self, steps: list[dict]) -> str:
        lines = [f"[{step['status']}] {step['title']}" for step in plan_payload(steps)]
        return PLAN_RESULT_PREFIX + "\n".join(lines)

    def _tool_update_goal(self, **args) -> str:
        goal = goal_payload(args)
        budget = goal["budget"]
        lines = [f"目标已更新（{goal['status']}）：{goal['objective']}",
                 f"预算：max_turns={budget['max_turns']}, max_tool_calls={budget['max_tool_calls']}"]
        if goal["acceptance_criteria"]:
            lines.append("验收条件：" + "；".join(goal["acceptance_criteria"]))
        if goal["stop_reason"]:
            lines.append("停止原因：" + goal["stop_reason"])
        return "\n".join(lines)

    def _tool_update_memory(self, action: str = "append", content: str = "") -> str:
        from .memory import MEMORY_PATH, append_memory, compact_memory
        if action not in {"append", "compact"}:
            raise ValueError("记忆操作只能是 append/compact")
        path = self._path(MEMORY_PATH)
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        updated = append_memory(current, content) if action == "append" else compact_memory(current)
        self._write_text(MEMORY_PATH, updated)
        return f"长期记忆已{('追加' if action == 'append' else '压缩')}：{len(updated.encode('utf-8'))} 字节"

    def _tool_get_skill(self, name: str) -> str:
        from .skills import get_skill
        sk = get_skill(name.lstrip("$"), self.user_id, self.org_id)
        if sk is None:
            from .skills import list_skills
            return f"技能 {name!r} 不存在。可用: {[s['name'] for s in list_skills(self.user_id, self.org_id)]}"
        return sk["body"]

    # 提案批准后真正写文件（由 approve 端点调用）
    async def execute_case_tool(self, db, case, name: str, args: dict,*,tool_call_id=None,authorization=None,cancel=None) -> dict:
        """Live Agent dispatch; read/derive/write memory under the case lock.

        Append and compact operate on the latest committed memory, so they do
        not use a stale model-read baseline. The caller commits with tool events.
        Native shell side effects execute outside the database write boundary,
        after persisting a tool-bound owner and registering its actual birth.
        """
        if name in {'read_text_file', 'list_files', 'search_text'}:
            from .agent_storage_capacity import read_case
            if case.id != self.case_id or case.s3_prefix != self.prefix:
                raise ValueError('Read tool belongs to another case')
            return await read_case(self.case_id, self.prefix, self.user_id, self.execute, name, args, cancel=cancel)
        if name == 'get_skill':
            return await finish_before_cancel(asyncio.to_thread(self.execute, name, args))
        if name in PROPOSAL_TOOLS:
            return await self.apply_proposal(db, case, name, args)
        if name=='run_shell_command':
            from .agent_shell import execute
            if case.id!=self.case_id or case.s3_prefix!=self.prefix:raise ValueError('Agent shell belongs to another case')
            await db.commit()
            return await execute(case.id,self.user_id,tool_call_id,authorization,args,cancel)
        if name == "update_memory":
            from .case_write import coordinate
            from .memory import write_memory
            if case.id != self.case_id or case.s3_prefix != self.prefix:
                raise ValueError("Memory belongs to another case")
            async with coordinate(db, case):
                if case.s3_prefix != self.prefix:
                    raise ValueError("Case storage location changed")
                return await write_memory(db, case, **args)
        return self.execute(name, args)

    async def apply_proposal(self, db, case, name: str, args: dict) -> dict:
        """Serialize HTTP/Agent writes and check the proposal again under the lock.

        Callers own authorization and the transaction commit. The synchronous
        helpers remain usable for isolated tooling, but live API callers must
        use this coordinator so the baseline check cannot race another writer.
        """
        from .case_write import coordinate
        if name not in PROPOSAL_TOOLS:
            raise ValueError("Unsupported file proposal")
        if case.id != self.case_id or case.s3_prefix != self.prefix:
            raise ValueError("Proposal belongs to another case")
        baseline = proposal_digest(args)
        conditions = {args["path"]: baseline}
        async with coordinate(db, case, expected_files=conditions):
            from .. import storage
            from .case_write import normalize_path
            from .casedict import CASE_DICT_PATH, write_case_dict
            from .file_transactions import write
            path = args['path']
            current = storage.get_bytes(self.prefix, path).decode('utf8') if name == 'propose_text_replacement' else None
            content, details = proposal_edit(name, args, current)
            if is_case_dictionary(self.prefix, path):
                await write_case_dict(db, case, content, source='agent', expected_files={CASE_DICT_PATH: baseline})
            else:
                normalized = normalize_path(path)
                await write(db, case, {normalized: content.encode('utf8')},
                            expected_files={normalized: baseline}, content_types={normalized: 'text/plain; charset=utf-8'})
        return {"ok":True, **details}

    def apply_file_update(self, path: str, content: str, baseline_sha256: str | None = None,
                          enforce_baseline: bool = False):
        if enforce_baseline:
            self._assert_baseline(path, baseline_sha256)
        self._write_text(path, content)

    def apply_text_replacement(self, path: str, old_text: str, new_text: str,
                               replace_all: bool = False, baseline_sha256: str | None = None,
                               enforce_baseline: bool = False) -> str:
        if enforce_baseline:
            self._assert_baseline(path, baseline_sha256)
        p = self._path(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        current = p.read_text(encoding="utf-8")
        n = current.count(old_text)
        if n == 0:
            raise ValueError("old_text 在文件中不存在（文件可能已被改动，需重新提案）")
        if n > 1 and not replace_all:
            raise ValueError(f"old_text 出现 {n} 次，非唯一（需 replace_all）")
        updated = current.replace(old_text, new_text) if replace_all \
            else current.replace(old_text, new_text, 1)
        self._write_text(path, updated)
        return f"{path} 已更新（{'全部' if replace_all else '单处'}替换 {n if replace_all else 1} 处）"
