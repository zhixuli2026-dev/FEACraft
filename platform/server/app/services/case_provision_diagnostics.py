"""Read one bounded page of retained account storage without recovering it."""
import base64
import heapq
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat

from fastapi import HTTPException
from sqlalchemy import select

from .. import storage
from ..config import get_settings
from ..models import Case
from .async_ops import finish_before_cancel
from .case_provision import owner_prefix
from .case_read_capacity import file_read_deadline
from .case_write import bounded_case_read
from .file_transactions import _path
from .storage_listing_budget import ListingBudget
from .storage_usage import _ordinary


def _encode_cursor(user_id, partition, after):
    value = json.dumps([1, user_id, partition, after], separators=(",", ":"), ensure_ascii=False).encode("utf8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(user_id, cursor):
    if cursor is None:
        return "private", ""
    try:
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        version, owner, partition, after = value
        if (type(version) is not int or version != 1 or type(owner) is not int or owner != user_id
                or partition not in {"private", "cases"} or not isinstance(after, str)
                or len(after.encode("utf8")) > 4096 or "\x00" in after):
            raise ValueError()
        return partition, after
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(422, {"code": "invalid_provision_cursor", "message": "存储诊断游标无效，请从第一页重试"}) from None


def _children(folder, relative, after, width, budget):
    def candidates(entries):
        for entry in entries:
            budget.visit()
            path = Path(entry.path)
            info = _ordinary(path)
            directory = stat.S_ISDIR(info.st_mode)
            key = relative + entry.name + ("/" if directory else "")
            if key > after or (directory and after.startswith(key)):
                yield key, path, info, directory

    with os.scandir(folder) as entries:
        # The containing directory may precede the cursor. It is traversed
        # without consuming a page slot, so reserve room for one such entry.
        return heapq.nsmallest(width + 1, candidates(entries), key=lambda row: row[0])


def _iter_filesystem(root, after, width, budget):
    try:
        root_info = _ordinary(root)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("Storage diagnostic root must be a directory")
    stack = [iter(_children(root, "", after, width, budget))]
    while stack:
        candidate = next(stack[-1], None)
        if candidate is None:
            stack.pop()
            continue
        key, path, observed, directory = candidate
        if key > after:
            yield key, None if directory else observed.st_size
        if directory:
            budget.check()
            current = _ordinary(path)
            if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                raise ValueError("Storage directory changed during diagnosis")
            stack.append(iter(_children(path, key, after, width, budget)))


def _objects(prefix, after, limit, budget):
    if not storage._use_s3():
        width = limit + 1
        root = _path(storage._fs_root(), prefix)
        nodes = list(islice(_iter_filesystem(root, after, width, budget), width))
        return nodes[:limit], len(nodes) > limit

    # Diagnose retained physical objects even when a case has a logical catalog.
    # Asking that catalog for one page would first collect the entire account.
    from .s3_live_store import Client

    client = storage.s3()
    if isinstance(client, Client):
        client = client.raw
    stem = storage._s3_key(prefix, "")
    response = client.list_objects_v2(Bucket=get_settings().s3_bucket, Prefix=stem, StartAfter=stem + after,
                                      MaxKeys=limit + 1)
    budget.check()
    nodes = []
    previous = stem + after
    for row in response.get("Contents", []):
        budget.visit()
        key = row["Key"]
        size = row["Size"]
        if not key.startswith(stem) or key <= previous or type(size) is not int or size < 0:
            raise ValueError("Invalid storage diagnostic page")
        previous = key
        nodes.append((key[len(stem):], None if key.endswith("/") else size))
    more = len(nodes) > limit or bool(response.get("IsTruncated"))
    if more and not nodes:
        raise ValueError("Storage diagnostic page did not advance")
    return nodes[:limit], more


def _page_objects(user_id, partition, after, limit):
    prefixes = {"private": owner_prefix(user_id), "cases": f"workspaces/{user_id}/cases"}
    budget = ListingBudget()
    nodes, more = _objects(prefixes[partition], after, limit, budget)
    found = [(partition, path, size) for path, size in nodes if size is not None]
    if more:
        return found, (partition, nodes[-1][0])
    if partition == "cases":
        return found, None
    remaining = limit - len(nodes)
    if not remaining:
        return found, ("cases", "")
    nodes, more = _objects(prefixes["cases"], "", remaining, budget)
    found.extend(("cases", path, size) for path, size in nodes if size is not None)
    return found, ("cases", nodes[-1][0]) if more else None


def _case_prefix(base, path):
    parts = path.split("/")
    if len(parts) >= 3 and re.fullmatch("[0-9a-f]{32}", parts[0]) and parts[1].isdigit():
        return base + "/" + "/".join(parts[:2])
    return base + "/" + parts[0]


async def diagnose_owner(db, user_id, *, cursor=None, limit=256):
    partition, after = _decode_cursor(user_id, cursor)
    base = f"workspaces/{user_id}/cases"
    counts = dict.fromkeys(("pending_records", "history_records", "staging_files", "staging_bytes",
                             "history_bytes", "reservation_records"), 0)
    unknown = {}
    record_types = {"pending": "pending_records", "history": "history_records", "staging": "staging_files",
                    "claims": "reservation_records"}
    async with file_read_deadline("provision_diagnostic", "存储诊断"):
        await finish_before_cancel(db.rollback())
        rows, successor = await bounded_case_read(_page_objects, user_id, partition, after, limit,
                                                   code="provision_diagnostic", label="存储诊断")
        for segment, path, size in rows:
            if segment == "cases":
                prefix = _case_prefix(base, path)
                item = unknown.setdefault(prefix, {"prefix": prefix, "files": 0, "bytes": 0})
                item["files"] += 1
                item["bytes"] += size
            else:
                group, separator, _ = path.partition("/")
                if separator and group in record_types:
                    counts[record_types[group]] += 1
                    if group in {"history", "staging"}:
                        counts[group + "_bytes"] += size
        if unknown:
            query = select(Case.s3_prefix).where(Case.s3_prefix.in_(list(unknown))).distinct()
            for prefix in await db.scalars(query):
                unknown.pop(prefix)

    prefixes = sorted(unknown)
    return {**counts, "unreferenced_prefixes": prefixes, "unreferenced_action": "retained",
            "unreferenced_inventory": [unknown[prefix] for prefix in prefixes],
            "history_retention_days": get_settings().provision_history_retention_days,
            "stage_retention_seconds": get_settings().provision_stage_retention_seconds,
            "inventory": "physical", "complete": cursor is None and successor is None,
            "next_cursor": _encode_cursor(user_id, *successor) if successor else None}
