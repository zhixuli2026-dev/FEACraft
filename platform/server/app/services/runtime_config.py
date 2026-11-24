"""管理员运行时 Setup：受限路径配置、原子持久化与默认值回退。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

_ALLOWED = {"python", "pandoc", "wsl_distro", "openfoam_bashrc"}
_ALLOWED_DISTROS = {"vibeflow-linux", "openfoam2406-default"}
_CONFIG_PATH = Path(os.getenv("VIBEFLOW_RUNTIME_CONFIG",
    Path(__file__).parent.parent.parent / "data" / "runtime-config.json"))
_DEFAULTS = {
    "wsl_distro": "vibeflow-linux",
    "openfoam_bashrc": "/usr/lib/openfoam/openfoam2406/etc/bashrc",
}


def path_binary(name: str, env: dict) -> str | None:
    # Passing an explicit empty path keeps an isolated environment isolated.
    return shutil.which(name, path=env.get("PATH", ""))


def _stored_settings() -> dict:
    try:
        stored = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise ValueError("runtime-config.json 损坏") from error
    return ({key: stored[key].strip() for key in _ALLOWED
             if isinstance(stored.get(key), str) and stored[key].strip()}
            if isinstance(stored, dict) else {})


def get_runtime_settings(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    values = dict(_DEFAULTS, python=sys.executable)
    values.update(_stored_settings())
    for key in _ALLOWED:
        explicit = env.get('VIBEFLOW_'+key.upper())
        if explicit and explicit.strip():
            values[key] = explicit.strip()
    if 'pandoc' not in values:
        pandoc = path_binary("pandoc", env)
        if not pandoc:
            candidate = Path("D:/vibeflow_reverse/extracted/share/vibeflow/tools/pandoc/pandoc.exe")
            pandoc = str(candidate) if candidate.is_file() else ""
        values['pandoc'] = pandoc
    for key in ('python', 'pandoc'):
        if values.get(key):
            try:
                values[key] = str(Path(values[key]).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                # Keep an invalid configured value for the capability error.
                pass
    return values


def get_runtime_setting(name: str, fallback: str) -> str:
    return get_runtime_settings().get(name) or fallback


def update_runtime_settings(patch: dict) -> dict:
    unknown = set(patch) - _ALLOWED
    if unknown:
        raise ValueError(f"不支持的 Setup 项: {sorted(unknown)}")
    current = _stored_settings()
    effective = get_runtime_settings()
    for key, value in patch.items():
        explicit = os.environ.get('VIBEFLOW_'+key.upper())
        if explicit and explicit.strip():
            # An API edit must not silently lose to an active deployment override.
            candidate = value.strip() if isinstance(value, str) else value
            if candidate and key in ('python', 'pandoc'):
                candidate = str(Path(candidate).expanduser().resolve())
            if candidate != effective[key]:
                raise ValueError(f'{key} 由 VIBEFLOW_{key.upper()} 管理，请修改部署环境变量')
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            current.pop(key, None)
            continue
        if not isinstance(value, str) or len(value.strip()) > 500:
            raise ValueError(f"{key} 必须为不超过 500 字符的路径/名称")
        value = value.strip()
        if key in ("python", "pandoc") and not Path(value).expanduser().is_file():
            raise ValueError(f"{key} 指向的文件不存在")
        if key in ('python', 'pandoc'):
            value = str(Path(value).expanduser().resolve())
        if key == "wsl_distro" and (not re.fullmatch(r"[a-z0-9-]+", value)
                                    or value not in _ALLOWED_DISTROS):
            raise ValueError("WSL 发行版不在当前授权集合")
        if key == "openfoam_bashrc":
            from .runtime_native import validate

            try:
                validate({"kind": "wsl", "bashrc": value})
            except ValueError as error:
                raise ValueError("OpenFOAM bashrc 必须位于 /usr/lib/openfoam/*/etc/bashrc") from error
        current[key] = value
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _CONFIG_PATH.with_name(f".{_CONFIG_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(current, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    return get_runtime_settings()
