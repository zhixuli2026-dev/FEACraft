"""VIBEFLOW_<TOOL> 环境注册表与 A–E/V 阶段最小能力检查。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from .cli_discovery import discover_cli
from .runtime_config import get_runtime_settings, path_binary


def _binary(name: str, env: dict, fallback: str | None) -> dict:
    key = f"VIBEFLOW_{name.upper()}"
    explicit = (env.get(key) or "").strip()
    value = explicit or fallback
    try:
        value = str(Path(value).expanduser().resolve()) if value else None
        available = bool(value and Path(value).is_file())
    except (OSError, RuntimeError, ValueError):
        available = False
    source = ("explicit" if available else "explicit_error") if explicit else ("fallback" if value else "missing")
    row = {"tool": name, "env": key, "value": value, "source": source, "available": available}
    if explicit and not available:
        row["error"] = "显式路径不可用"
    return row


def _host_tools(env, settings):
    return {
        'python': _binary('python', env, settings['python']),
        'node': _binary('node', env, path_binary('node', env)),
        'npm': _binary('npm', env, path_binary('npm', env)),
        'pandoc': _binary('pandoc', env, settings['pandoc']),
    }


def cli_details(env):
    details = {}
    for tool in ("claude", "codex", "opencode", "kimi"):
        key = 'VIBEFLOW_' + tool.upper()
        try:
            details[tool] = discover_cli(tool, env=env)
        except (OSError, ValueError, RuntimeError) as error:
            details[tool] = {'path': None, 'source': 'explicit_error' if env.get(key) else 'missing',
                             'error': str(error)[:160]}
    return details


def _linux_solver(name, env, configured):
    key = 'VIBEFLOW_'+name.upper()
    try:
        path,runtime = configured(env)
        return {'tool':name,'env':key,'value':path,'runtime':runtime,
                'source':'explicit' if env.get(key) else 'fallback','available':True}
    except FileNotFoundError as error:
        return {'tool':name,'env':key,'value':env.get(key),
                'source':'explicit_error' if env.get(key) else 'missing','available':False,'error':str(error)}


def environment_registry(env: dict | None = None, *, settings=None, cli=None, wsl=None) -> list[dict]:
    env = os.environ if env is None else env
    settings = get_runtime_settings(env) if settings is None else settings
    cli = cli_details(env) if cli is None else cli
    from .calculix import default_executable as calculix_default
    from .su2 import default_executable as su2_default
    from .elmer import default_executable as elmer_default
    from .moose import configured as moose_configured
    from .code_aster import configured as aster_configured
    from .paraview import default_executable as paraview_default
    calculix_path = calculix_default(env)
    su2_path = su2_default(env)
    elmer_path = elmer_default(env)
    paraview_paths = {name: paraview_default(name, env) for name in ('pvpython', 'pvbatch')}
    entries = [
        *_host_tools(env, settings).values(),
        _binary("calculix", env, str(calculix_path) if calculix_path else None),
        _binary("su2", env, str(su2_path) if su2_path else None),
        _binary("elmer", env, str(elmer_path) if elmer_path else None),
        _linux_solver('moose',env,moose_configured),
        _linux_solver('code_aster',env,aster_configured),
        *[_binary(name, env, str(path) if path else None) for name, path in paraview_paths.items()],
    ]
    for tool, observed in (('wsl_distro','reachable'), ('openfoam_bashrc','openfoam_bashrc_loaded')):
        key = 'VIBEFLOW_' + tool.upper()
        available = bool(wsl and wsl.get(observed))
        explicit = bool((env.get(key) or '').strip())
        source = 'explicit' if explicit else 'fallback'
        if wsl is not None and explicit and not available:
            source = 'explicit_error'
        value = wsl.get('distro', settings[tool]) if wsl and tool == 'wsl_distro' else settings[tool]
        entries.append({'tool':tool,'env':key,'value':value,'source':source,'available':available})
    for tool, info in cli.items():
        key = 'VIBEFLOW_' + tool.upper()
        info = info or {}
        path = info.get('path')
        row = {"tool":tool,"env":key,"value":path or env.get(key),"source":info.get('source','missing'),
               "available":bool(path)}
        if info.get('error'):
            row['error'] = info['error']
        entries.append(row)
    return entries


def _packages() -> dict[str, bool]:
    return {name: importlib.util.find_spec(name) is not None
            for name in ("cadquery", "vtk", "numpy", "matplotlib")}


def stage_check(stage: str, wsl: dict, packages: dict[str, bool] | None = None, *, tools=None) -> dict:
    packages = _packages() if packages is None else packages
    if tools is None:
        tools = _host_tools(os.environ, get_runtime_settings())
    pandoc_available = tools['pandoc']['available']
    checks = {
        "A": [("geometry/CadQuery", packages.get("cadquery", False))],
        "B": [("mesh/OpenFOAM", bool(wsl.get("openfoam")))],
        "C": [("solve/OpenFOAM", bool(wsl.get("openfoam"))),
              ("UDF/wmake", bool(wsl.get("wmake")))],
        "D": [("post/VTK", packages.get("vtk", False)),
              ("post/numpy", packages.get("numpy", False)),
              ("plot/matplotlib", packages.get("matplotlib", False)),
              ("report/pandoc", pandoc_available)],
        "E": [("report/pandoc", pandoc_available),
              ("LaTeX/latexmk", bool(wsl.get("latexmk")))],
        "V": [("dev/node", tools['node']['available']), ("dev/npm", tools['npm']['available'])],
    }
    if stage not in checks:
        raise ValueError(f"未知阶段 {stage}")
    stage_checks = [{"check": name, "ok": ok} for name, ok in checks[stage]]
    return {"stage": stage, "ok": all(item["ok"] for item in stage_checks),
            "missing": [item["check"] for item in stage_checks if not item["ok"]],
            "checks": stage_checks}


def stage_checks(wsl: dict, packages: dict[str, bool] | None = None, *, tools=None) -> dict:
    if tools is None:
        tools = _host_tools(os.environ, get_runtime_settings())
    packages = _packages() if packages is None else packages
    return {stage: stage_check(stage, wsl, packages, tools=tools) for stage in ("A", "B", "C", "D", "E", "V")}
