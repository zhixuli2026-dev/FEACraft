"""Resolve installed ACP agents and build isolated, profile-bound environments."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import uuid

from .acp_configuration import HARNESSES, environment as build_environment
from .cli_discovery import discover_cli
from .codex_environment import OS_ENVIRONMENT


NETWORK_ENVIRONMENT = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}


def command(harness):
    if harness not in HARNESSES:
        raise ValueError('Unsupported ACP harness')
    found = discover_cli(harness)
    if found is None:
        raise FileNotFoundError('ACP harness is not installed')
    executable = Path(found['path']).resolve(strict=True)
    if os.name != 'nt' or executable.suffix.lower() == '.exe':
        return [str(executable), 'acp']

    # The Windows npm shims are batch files. Use their installed package
    # entrypoints directly so there is no shell parsing of launch arguments.
    packages = executable.parent/'node_modules'
    if harness == 'opencode':
        program = (packages/'@opencode/cli/bin/opencode.exe').resolve(strict=True)
        return [str(program), 'acp']
    node = shutil.which('node.exe')
    if not node:
        raise FileNotFoundError('ACP harness requires Node.js')
    program = (packages/'@moonshot-ai/kimi-code/dist/main.mjs').resolve(strict=True)
    return [str(Path(node).resolve(strict=True)), str(program), 'acp']


def environment(harness, home, *, endpoint, model, api_key, wire='openai', allow_file_approval=False,
                managed_text_only=False, contract_file=None):
    from .file_transactions import _path, _write_new

    def write_config(directory, name, content):
        path = _path(directory, name)
        temporary = _path(directory, 'config-'+uuid.uuid4().hex+'.tmp')
        try:
            _write_new(temporary, content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    env = {key:value for key, value in os.environ.items() if key.upper() in OS_ENVIRONMENT | NETWORK_ENVIRONMENT}
    return build_environment(harness, Path(home).resolve(), env, endpoint=endpoint, model=model, api_key=api_key,
        wire=wire, allow_file_approval=allow_file_approval, managed_text_only=managed_text_only,
        contract_file=contract_file, write_config=write_config)
