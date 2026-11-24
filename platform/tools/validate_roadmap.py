"""Validate delivery documents against all audit IDs and mainline dependencies.

--render only refreshes the generated Markdown appendix from the JSON ledger;
it never changes task completion or audit evidence states.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote

from validate_audit import parse_plan

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
LEDGER = DOCS / "full-feature-tasks.json"
PLAN = DOCS / "full-function-delivery-plan.md"
STAGE = DOCS / "next-stage-mainline-plan.md"
START, END = "<!-- full-feature-ledger:start -->", "<!-- full-feature-ledger:end -->"
ARCHIVE_INDEX = DOCS / "historical-evidence.sha256"
DOMAIN_NAMES = {"A":"项目与工程模型", "B":"几何 CAD 和网格", "C":"物理 求解和分析",
    "D":"可视化和后处理", "E":"报告 文档和演示", "F":"Agent 和技能", "G":"终端和运行时",
    "H":"作业 日志 索引和事件", "I":"转换与远程资源", "J":"团队与生产运维"}


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def scope_exclusions(data):
    return [identifier for decision in data.get('scope_exclusions', []) for identifier in decision['task_ids']]


def render(data):
    parts = []
    exclusions = set(scope_exclusions(data))
    for domain, name in DOMAIN_NAMES.items():
        tasks = [task for task in data["tasks"] if task["domain"] == domain]
        parts += [f"### {domain} {name}\n\n",
                  "| 任务与功能 | 当前证据 | 主 WP / 波次 | 后续工作 | 验收包 |\n",
                  "|---|---|---|---|---|\n"]
        for task in tasks:
            work = '本次交付已移出，保留原审计与验收记录。' if task['task_id'] in exclusions else task['required_work']
            parts.append("| " + " | ".join([
                f"`{task['task_id']}` {cell(task['title'])}", task["baseline_status"],
                f"{task['work_package']} / {task['target_wave']}", cell(work),
                task["acceptance_pack"]]) + " |\n")
        parts.append("\n")
    return "".join(parts)


def archive_paths():
    indexed = set()
    for line in ARCHIVE_INDEX.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, relative = line.split("  ", 1)
        path = Path(relative)
        if (not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not relative.startswith(("tmp/", "platform/backups/", ".impeccable/review/"))
                or ".." in path.parts or path.is_absolute() or path in indexed):
            raise SystemExit("Invalid historical evidence index entry: " + relative)
        indexed.add(path)
    return indexed


def validate(data, *, source_only=False):
    errors = []
    archived = archive_paths() if source_only else set()
    absent_evidence = set()

    def local_link_present(path):
        if path.exists():
            return True
        absolute = Path(os.path.abspath(path))
        if absolute.is_relative_to(ROOT.parent):
            relative = absolute.relative_to(ROOT.parent)
            if relative in archived:
                absent_evidence.add(relative)
                return True
        return False

    audit_bytes = (DOCS / "feature-gap-audit.md").read_bytes()
    audit = {}
    for line in audit_bytes.decode("utf-8").splitlines():
        match = re.match(r"^\| ([A-Z][A-Z0-9]*) \| (.*?) \| (✅|🟡|🧪|⬜|⏸|❓) \|", line)
        if match:
            audit[match[1]] = {"title": match[2], "status": match[3]}
    owners = dict(parse_plan())
    tasks = data["tasks"]
    ids = [task["audit_id"] for task in tasks]
    task_ids = [task["task_id"] for task in tasks]
    excluded = scope_exclusions(data)
    excluded_ids = set(excluded)
    if excluded_ids - set(task_ids) or len(excluded) != len(excluded_ids):
        errors.append('Delivery scope exclusions must refer to unique audit task IDs')
    active_tasks = [task for task in tasks if task['task_id'] not in excluded_ids]
    if len(ids) != 295 or set(ids) != set(audit) or len(set(ids)) != len(ids):
        errors.append("Ledger must cover every one of the 295 audit IDs exactly once")
    if data.get("source_audit_sha256") != hashlib.sha256(audit_bytes).hexdigest():
        errors.append("Audit changed since ledger snapshot; reconcile evidence fields and source hash")
    expected_counts = dict(Counter(item["status"] for item in audit.values()))
    if data["baseline_counts"] != expected_counts:
        errors.append("Baseline status counts differ from the audit")
    if data["total_tasks"] != len(tasks) or data["unclosed_baseline_items"] != sum(t["status"]!="✅" for t in audit.values()):
        errors.append("Ledger total/unclosed counts differ from actual rows")
    stage_text = STAGE.read_text(encoding="utf-8")
    stage_rows = {}
    for line in stage_text.splitlines():
        match = re.match(r"^\| (N\d-\d{2}) \|", line)
        if match:
            if match[1] in stage_rows:
                errors.append("Duplicate mainline task: " + match[1])
            columns = line.split("|")
            stage_rows[match[1]] = re.findall(r"N\d-\d{2}", columns[-2])
    if len(stage_rows) != 53 or set(stage_rows) != set(data["next_stage_tasks"]):
        errors.append("Mainline plan and ledger must contain exactly 53 matching tasks")
    scenarios = re.findall(r"^\| (V\d{2}) \|", stage_text, re.M)
    if len(scenarios) != len(set(scenarios)) or set(scenarios) != {f"V{n:02d}" for n in range(1,25)}:
        errors.append("Mainline fault matrix must contain V01-V24 exactly once")
    for task in tasks:
        item = task["audit_id"]
        source = audit.get(item, {})
        if task["task_id"] != "FF-" + item or task["work_package"] != owners.get(item):
            errors.append("Task ID/work package mismatch: " + item)
        if task["title"] != source.get("title") or task["baseline_status"] != source.get("status"):
            errors.append("Audit snapshot mismatch: " + item)
        if not task["required_work"].strip() or not re.fullmatch(r"T(?:0[1-9]|10)", task["acceptance_pack"]):
            errors.append("Missing task work/acceptance: " + item)
        if not re.fullmatch(r"R[0-7]", task["target_wave"]):
            errors.append("Unknown delivery wave: " + item)
        if set(task["depends_on"]) - set(task_ids):
            errors.append("Unknown feature dependency: " + item)
        if set(task["next_stage_tasks"]) - set(stage_rows):
            errors.append("Unknown mainline subtask: " + item)
        if task["baseline_status"] == "⏸" and not any(word in task["delivery_status"] for word in ("恢复", "新增扩展")):
            errors.append("Historically paused scope was excluded: " + item)
        if task["completed"] and not task["implementation_evidence"]:
            errors.append("Completed task lacks implementation evidence: " + item)

    def check_cycles(graph, label):
        visiting, visited = set(), set()
        def visit(item):
            if item in visiting:
                errors.append(label + " dependency cycle: " + item)
                return
            if item in visited:
                return
            visiting.add(item)
            for dependency in graph.get(item, []):
                if dependency not in graph:
                    errors.append(label + " unknown dependency: " + dependency)
                else:
                    visit(dependency)
            visiting.remove(item)
            visited.add(item)
        for item in graph:
            visit(item)
    check_cycles(stage_rows, "Mainline")
    check_cycles({t["task_id"]:t["depends_on"] for t in tasks}, "Feature")
    decisions = data["decision_reviews"]
    status_path = DOCS / "mainline-task-status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    states = {item["task_id"]: item for item in status["tasks"]}
    if len(status["tasks"]) != 53 or set(states) != set(stage_rows):
        errors.append("Mainline status ledger must cover all 53 tasks exactly once")
    for identifier, item in states.items():
        if item["status"] not in {"pending", "in_progress", "complete"}:
            errors.append("Unknown task status: " + identifier)
        if set(item["depends_on"]) != set(stage_rows.get(identifier, [])):
            errors.append("Status ledger dependency mismatch: " + identifier)
        if item["status"] == "complete":
            if not item["implementation_evidence"] or not item.get("completed_at"):
                errors.append("Completed mainline task lacks evidence/date: " + identifier)
            for dependency in item["depends_on"]:
                if states.get(dependency, {}).get("status") != "complete":
                    errors.append("Completed task has unfinished dependency: " + identifier)
        if item["status"] == "in_progress" and not item["remaining"]:
            errors.append("Partial task must name remaining work: " + identifier)
        for evidence in item["implementation_evidence"]:
            if not local_link_present(DOCS / evidence):
                errors.append("Missing mainline evidence: " + evidence)
    fault_rows = status["fault_matrix"]
    if len(fault_rows) != 24 or {item["id"] for item in fault_rows} != set(scenarios):
        errors.append("Fault execution status must cover V01-V24 exactly once")
    for item in fault_rows:
        if item["status"] == "complete" and not item["implementation_evidence"]:
            errors.append("Completed fault scenario lacks evidence: " + item["id"])
    if len(decisions) != 12 or {d["id"] for d in decisions} != {f"K{n:02d}" for n in range(1,13)}:
        errors.append("Decision reviews must cover K01-K12")
    text = PLAN.read_text(encoding="utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        errors.append("Missing or duplicate Markdown ledger markers")
    else:
        actual = text.split(START,1)[1].split(END,1)[0].strip()
        if actual != render(data).strip():
            errors.append("Markdown appendix differs from JSON; run --render after reconciling tasks")
    for path in (PLAN, STAGE, DOCS / "README.md"):
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("http:", "https:", "mailto:", "#")):
                continue
            relative = unquote(target.split("#",1)[0].strip("<>"))
            if relative and not local_link_present(path.parent / relative):
                errors.append(f"Missing link in {path.name}: {relative}")
    if errors:
        raise SystemExit("\n".join(errors))
    remaining = Counter(task['target_wave'] for task in active_tasks if not task['completed'])
    feature_execution = {'scope_total':len(active_tasks), 'complete':len(active_tasks)-sum(remaining.values()),
                         'remaining':sum(remaining.values()), 'remaining_by_wave':dict(sorted(remaining.items())),
                         'excluded':len(excluded), 'historically_complete':sum(task['completed'] for task in tasks)}
    return {"status":"passed", "audit_ids":len(ids), "mainline_tasks":len(stage_rows),
            "fault_scenarios":len(scenarios), "historical_decisions":len(decisions),
            "baseline_status_counts":expected_counts, "dependency_cycles":0, "missing_local_links":0,
            "mainline_execution":dict(Counter(item["status"] for item in states.values())),
            "evidence_check": "index" if source_only else "local",
            "indexed_evidence_not_present":len(absent_evidence),
            "feature_execution":feature_execution}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render", action="store_true", help="Refresh only the generated Markdown task table")
    parser.add_argument("--report", type=Path, help="Optional JSON validation artifact")
    parser.add_argument("--source-only", action="store_true",
                        help="Check a Git clone against the historical evidence index")
    args = parser.parse_args()
    data = json.loads(LEDGER.read_text(encoding="utf-8"))
    if args.render:
        text = PLAN.read_text(encoding="utf-8")
        assert text.count(START) == text.count(END) == 1
        before, rest = text.split(START,1)
        _, after = rest.split(END,1)
        PLAN.write_text(before + START + "\n\n" + render(data) + END + after, encoding="utf-8")
    result = validate(data, source_only=args.source_only)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("ROADMAP_OK " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
