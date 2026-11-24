"""校验功能审计与总体计划：ID/计数/领域分布/工作包映射/本地链接。"""
from collections import Counter
from pathlib import Path
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
AUDIT = DOCS / "feature-gap-audit.md"
PLAN = DOCS / "implementation-master-plan.md"
STATUS_NAMES = ("✅", "🟡", "🧪", "⬜", "⏸", "❓")


def parse_audit():
    section = None
    statuses = Counter()
    domains = {}
    ids = []
    for line in AUDIT.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"### ([A-K])\.", line)
        if heading:
            section = heading.group(1)
            domains.setdefault(section, Counter())
        row = re.match(r"\| ([A-Z][A-Z0-9]*) \|.*\| (✅|🟡|🧪|⬜|⏸|❓) \|", line)
        if row:
            item_id, status = row.groups()
            ids.append(item_id)
            statuses[status] += 1
            domains[section][status] += 1
    return ids, statuses, domains


def expand_token(token):
    match = re.fullmatch(r"([A-Z]+)(\d+)[–-](?:([A-Z]+))?(\d+)([A-Z]?)", token)
    if not match:
        return [token]
    prefix, start, end_prefix, end, suffix = match.groups()
    if prefix != (end_prefix or prefix) or suffix:
        raise ValueError(f"不支持的 ID 区间: {token}")
    width = len(start)
    return [f"{prefix}{number:0{width}d}" for number in range(int(start), int(end) + 1)]


def parse_plan():
    lines = PLAN.read_text(encoding="utf-8").splitlines()
    in_appendix = False
    owners = []
    for line in lines:
        if line.startswith("## 14."):
            in_appendix = True
            continue
        if in_appendix and line.startswith("## "):
            break
        row = re.match(r"\| (WP\d+) \| (.+) \|", line) if in_appendix else None
        if row:
            wp, body = row.groups()
            for token in body.split("、"):
                owners.extend((item, wp) for item in expand_token(token.strip()))
    return owners


def check_links():
    missing = []
    for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", PLAN.read_text(encoding="utf-8")):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = unquote(target.split("#", 1)[0])
        if target and not (DOCS / target).resolve().exists():
            missing.append(target)
    return missing


def main():
    ids, statuses, domains = parse_audit()
    errors = []
    duplicates = sorted(item for item, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"审计重复 ID: {duplicates}")
    text = AUDIT.read_text(encoding="utf-8")
    total = re.search(r"包含 \*\*(\d+) 个功能项", text)
    if not total or int(total.group(1)) != len(ids):
        errors.append(f"功能项总数不一致: 声明 {total.group(1) if total else '?'} / 实际 {len(ids)}")
    for status in STATUS_NAMES:
        row = re.search(rf"\| {re.escape(status)}[^|]*\| (\d+) \|", text)
        if not row or int(row.group(1)) != statuses[status]:
            errors.append(f"状态 {status} 计数不一致: 声明 {row.group(1) if row else '?'} / 实际 {statuses[status]}")
    distribution = re.search(r"领域分布\*\*：(.+)。", text)
    if distribution:
        declared = {domain: int(count) for domain, count in re.findall(r"([A-J]) (\d+) 项", distribution.group(1))}
        actual = {domain: domains[domain]["⬜"] for domain in "ABCDEFGHIJ"}
        if declared != actual:
            errors.append(f"领域分布不一致: 声明 {declared} / 实际 {actual}")
    else:
        errors.append("缺少领域分布声明")

    owners = parse_plan()
    owner_counts = Counter(item for item, _ in owners)
    owned = set(owner_counts)
    missing = sorted(set(ids) - owned)
    extra = sorted(owned - set(ids))
    repeated = sorted(item for item, count in owner_counts.items() if count > 1)
    if missing or extra or repeated:
        errors.append(f"计划映射: MISSING={missing}, EXTRA={extra}, DUPLICATES={repeated}")
    missing_links = check_links()
    if missing_links:
        errors.append(f"计划本地链接缺失: {missing_links}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"AUDIT_OK {len(ids)} IDs {dict(statuses)}")
    print(f"PLAN_OK {len(owners)} mappings; local links valid")
    print("UNIMPLEMENTED_BY_DOMAIN", {domain: domains[domain]["⬜"] for domain in "ABCDEFGHIJ"})


if __name__ == "__main__":
    main()
