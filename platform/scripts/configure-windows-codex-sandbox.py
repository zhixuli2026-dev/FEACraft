"""Inspect or initialize the installed Codex Windows sandbox from the host console."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))

from app.services.codex_environment import OS_ENVIRONMENT, create_isolated_codex_environment
from app.services.codex_sandbox import CodexSandboxSession
from app.services.codex_stdio import CodexRpcError, CodexStdioClient


def deployment_environment(home):
    if home == (Path.home() / '.codex').resolve():
        raise ValueError('Select the VibeFlow deployment CODEX_HOME')
    if not home.exists():
        return create_isolated_codex_environment(home)
    if not (home / 'config.toml').is_file():
        raise ValueError('The selected deployment home has no config.toml')
    env = {name: value for name, value in os.environ.items() if name.upper() in OS_ENVIRONMENT}
    env['CODEX_HOME'] = str(home)
    return env


def failure_code(error):
    detail = str(error).casefold()
    if '1223' in detail or 'cancelled' in detail or 'canceled' in detail or 'declined' in detail:
        return 'setup_declined'
    if '1385' in detail or 'logon type' in detail:
        return 'windows_logon_denied'
    if 'access is denied' in detail or 'permission denied' in detail:
        return 'windows_permission_denied'
    return 'setup_failed'


async def inspect(executable, home, mode, apply, timeout):
    env = deployment_environment(home)
    cwd = home
    if apply:
        cwd = home / '.vibeflow-sandbox-work'
        cwd.mkdir(exist_ok=True)
    args = [str(executable)]
    if mode is not None:
        args.extend(['-c', f'windows.sandbox="{mode}"'])
    args.extend(['app-server', '--listen', 'stdio://'])
    client = CodexStdioClient(args, env, cwd=cwd)
    status = {'operation': 'setup' if apply else 'status', 'mode': mode, 'actual_model_calls': 0}
    try:
        await client.start()
        setup = CodexSandboxSession(client)
        status['before'] = await setup.readiness()
        if apply:
            try:
                async with asyncio.timeout(timeout):
                    await setup.setup(mode, str(cwd), confirmed=True)
                    while not setup.handle_event(await client.next_event(timeout_seconds=timeout)):
                        pass
                status['completion'] = 'succeeded' if setup.status == 'ready' else failure_code(setup.error)
            except TimeoutError:
                status['completion'] = 'outcome_unconfirmed'
            except (CodexRpcError,RuntimeError) as error:
                status['completion'] = failure_code(error)
            status['after'] = await setup.readiness()
    except (CodexRpcError, OSError, RuntimeError, ValueError, TimeoutError):
        status['error'] = 'sandbox_status_unavailable'
    finally:
        await client.close()
    status['transport_stopped'] = bool(client.stop_result and client.stop_result.get('verified'))
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('status', 'setup'))
    parser.add_argument('--home', type=Path, required=True, help='Private deployment CODEX_HOME')
    parser.add_argument('--codex', type=Path, help='Installed Windows codex.exe')
    parser.add_argument('--mode', choices=('elevated', 'unelevated'))
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Run this operation from the Windows deployment host')
    if not 10 <= args.timeout <= 600:
        parser.error('Timeout must be 10 to 600 seconds')
    if args.operation == 'setup' and args.mode is None:
        parser.error('Select the initialization mode explicitly with --mode')
    executable = args.codex or shutil.which('codex.exe')
    if executable is None:
        parser.error('Specify the installed native codex.exe with --codex')
    try:
        executable = Path(executable).resolve(strict=True)
        if not executable.is_file() or executable.suffix.casefold() != '.exe':
            parser.error('Select a native Windows executable')
        result = asyncio.run(inspect(executable, args.home.resolve(), args.mode, args.operation == 'setup', args.timeout))
    except (OSError, ValueError) as error:
        parser.exit(2, f'Windows sandbox preparation failed: {error}\n')
    print(json.dumps(result))
    if (result.get('error') or not result['transport_stopped']
            or (args.operation == 'setup' and result.get('completion') != 'succeeded')):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
