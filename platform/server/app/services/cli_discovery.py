"""CLI agent 可执行文件发现：显式路径、版本目录、PATH 后缀与 WSL Node。"""
from __future__ import annotations

import os
from pathlib import Path
import re

SUPPORTED_TOOLS = {"codex", "claude", "opencode", "kimi"}
_SUFFIX_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}


def version_sort_key(value: str):
    match = re.search(r"(\d+(?:\.\d+){0,3})(?:[-.]?([A-Za-z]+)[-.]?(\d+)?)?", value)
    if not match:
        return (0, 0, -1, 0)
    numeric = tuple(int(part) for part in match.group(1).split("."))
    numeric += (0,) * (4 - len(numeric))
    suffix = (match.group(2) or "").lower()
    if not suffix:
        return (*numeric, 1, 99, 0)
    return (*numeric, 0, _SUFFIX_RANK.get(suffix, -1), int(match.group(3) or 0))


def _candidates(directory: Path, tool: str):
    for suffix in (".exe", ".cmd", ".bat", ""):
        candidate = directory / f"{tool}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def discover_cli(tool: str, env: dict | None = None, path_dirs: list[str] | None = None,
                 wsl_runner=None, distro: str = "vibeflow-linux") -> dict | None:
    if tool not in SUPPORTED_TOOLS:
        raise ValueError(f"不支持的 CLI 工具: {tool}")
    if wsl_runner is not None and not callable(wsl_runner):
        raise ValueError('WSL CLI discovery requires an explicit execution callback')
    env = os.environ if env is None else env
    key = f"VIBEFLOW_{tool.upper()}"
    explicit = env.get(key)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{key} 指向的文件不存在: {path}")
        return {"path": str(path.resolve()), "source": "explicit", "version": None, "runtime": "windows"}

    version_root = env.get(f"{key}_DIR")
    if version_root:
        root = Path(version_root).expanduser()
        candidates = []
        if root.is_dir():
            for directory in [root, *[p for p in root.iterdir() if p.is_dir()]]:
                executable = _candidates(directory / "bin", tool)
                if executable is None and directory == root:
                    executable = _candidates(directory, tool)
                if executable is not None:
                    version = directory.name if directory != root else None
                    candidates.append((version_sort_key(version or ""), executable, version))
        if candidates:
            _, executable, version = max(candidates, key=lambda item: item[0])
            return {"path": str(executable.resolve()), "source": "version_dir",
                    "version": version, "runtime": "windows"}

    directories = path_dirs if path_dirs is not None else env.get("PATH", "").split(os.pathsep)
    for directory in directories:
        if not directory:
            continue
        executable = _candidates(Path(directory), tool)
        if executable is not None:
            return {"path": str(executable.resolve()), "source": "path", "version": None, "runtime": "windows"}

    if wsl_runner is not None:
        runner = wsl_runner
        found = runner(["wsl.exe", "-d", distro, "--exec", "bash", "-lc", f"command -v {tool}"])
        if found.get("returncode") == 0 and found.get("stdout", "").strip():
            node = runner(["wsl.exe", "-d", distro, "--exec", "bash", "-lc", "node --version"])
            return {"path": found["stdout"].strip().splitlines()[0], "source": "wsl",
                    "version": None, "runtime": "wsl",
                    "node_version": node.get("stdout", "").strip() if node.get("returncode") == 0 else None}
    return None
