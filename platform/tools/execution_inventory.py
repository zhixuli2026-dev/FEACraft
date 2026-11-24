"""Inventory API routes and execution/cancellation/write boundaries from current AST.

Static call sites complement the reviewed entry matrix; they are not a call-graph
proof for dynamic dispatch, native libraries or user-supplied commands.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/execution-entry-inventory.json"


def category(name):
    tail = name.rsplit(".", 1)[-1]
    if tail in {"start_job", "start_batch", "enqueue", "run_remote", "run_job"}:
        return "submission"
    if tail in {"create_subprocess_exec", "create_subprocess_shell", "Popen", "spawn"} or name == "subprocess.run":
        return "process"
    if tail in {"cancel", "kill", "terminate", "cancel_case", "cancel_active", "supervisor_cancel_case"}:
        return "cancellation"
    if tail in {"publish_report_artifacts", "publish_latex_artifacts", "replace", "put_text", "put_bytes",
                "write_text", "write_bytes", "sync_job_staging", "cleanup_job_staging"}:
        return "publication_or_write"
    if tail in {"supervisor_register", "supervisor_unregister", "register", "unregister"}:
        return "supervision"
    return None


class Visitor(ast.NodeVisitor):
    def __init__(self, path):
        self.path, self.scope, self.routes, self.calls = path, [], [], []

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not ast.unparse(decorator.func).startswith("router."):
                continue
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                self.routes.append({"file": self.path, "function": ".".join(self.scope), "line": node.lineno,
                    "method": ast.unparse(decorator.func).split(".")[-1], "path": decorator.args[0].value})
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node):
        name = ast.unparse(node.func)
        kind = category(name)
        if kind:
            self.calls.append({"file": self.path, "function": ".".join(self.scope), "line": node.lineno,
                               "category": kind, "callee": name})
        self.generic_visit(node)


def inventory():
    files, calls, routes = {}, [], []
    for path in sorted((ROOT / "server/app").rglob("*.py")):
        name = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        files[name] = hashlib.sha256(data).hexdigest()
        visitor = Visitor(name)
        visitor.visit(ast.parse(data.decode("utf-8-sig")))
        calls.extend(visitor.calls)
        routes.extend(visitor.routes)
    return {"schema": "vibeflow.execution-inventory.v1", "scope": "server/app/**/*.py",
            "review_matrix": "execution-entry-matrix.md", "source_sha256": files,
            "routes": routes, "boundaries": calls,
            "limitations": ["Router paths are local; prepend each router prefix",
                "Native/WSL/MPI child processes and user shell code require runtime inspection",
                "Cancellation call sites include asyncio tasks; reviewed matrix distinguishes them",
                "Write boundaries include staging/checkpoints, not only active publication"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = inventory()
    if args.write:
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif not OUTPUT.exists() or json.loads(OUTPUT.read_text(encoding="utf-8")) != result:
        raise SystemExit("Execution inventory stale; review changed entries and run --write")
    print(f"EXECUTION_INVENTORY_OK sources={len(result['source_sha256'])} "
          f"routes={len(result['routes'])} boundaries={len(result['boundaries'])}")


if __name__ == "__main__":
    main()
