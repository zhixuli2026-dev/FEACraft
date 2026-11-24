"""Create a fresh, file-auth Codex home without inherited provider secrets."""
import os
from pathlib import Path

OS_ENVIRONMENT = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
                  'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROGRAMDATA', 'COMSPEC'}


def create_isolated_codex_environment(home):
    path = Path(home).resolve()
    if path == (Path.home() / '.codex').resolve():
        raise ValueError('A product session must not reuse the personal Codex home')
    # Exclusive creation makes this a new-home operation, never a credential
    # import or an overwrite of existing user configuration.
    path.mkdir(parents=True, exist_ok=False)
    (path / 'config.toml').write_text('cli_auth_credentials_store = "file"\n', encoding='utf8')
    environment = {key: value for key, value in os.environ.items() if key.upper() in OS_ENVIRONMENT}
    environment['CODEX_HOME'] = str(path)
    return environment


def wsl_codex_environment(environment, distro, *, native_state=None):
    """Explicit Win→WSL exports; credential values never enter argv or WSLENV."""
    if not isinstance(environment, dict):
        raise ValueError('WSL CLI requires an explicit private environment')
    from .codex_wsl_state import home_path, mounted_path
    expected = None
    if native_state is not None:
        home = home_path(native_state)
        expected = {'CODEX_HOME': home, 'HOME': home.rsplit('/', 1)[0], 'TMPDIR': home.rsplit('/', 1)[0] + '/tmp'}
    selected = {key: value for key, value in environment.items() if key.upper() in OS_ENVIRONMENT}
    for name in ('CODEX_HOME', 'HOME', 'TMPDIR'):
        value = environment.get(name)
        if (not isinstance(value, str) or not Path(value).is_absolute()
                or (expected is None and not Path(value).is_dir())):
            raise ValueError('WSL CLI requires existing private directories')
        selected[name] = value
        selected['VIBEFLOW_CLI_' + name] = mounted_path(value, distro)
        if expected is not None and selected['VIBEFLOW_CLI_' + name] != expected[name]:
            raise ValueError('WSL initialization crosses its selected private directories')
    # WSL's --user selection overwrites HOME after processing WSLENV. Carry
    # directories under private names and assign them only in the fixed bridge.
    exports = ['VIBEFLOW_CLI_CODEX_HOME/u', 'VIBEFLOW_CLI_HOME/u', 'VIBEFLOW_CLI_TMPDIR/u']
    selected['WSLENV'] = ':'.join(exports)
    return selected
