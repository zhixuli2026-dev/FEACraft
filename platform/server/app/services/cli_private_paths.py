"""One configured host CLI state root, outside public and operator auth storage."""
from pathlib import Path


def root(settings):
    public=Path(settings.storage_fs_root).resolve()
    base=Path(settings.codex_state_root).resolve() if settings.codex_state_root else public.parent
    selected=base/'.private-cli'
    if (selected.resolve()!=selected or selected.is_relative_to(public)
            or selected.is_relative_to((Path.home()/'.codex').resolve())):
        raise ValueError('CLI private state must remain outside public or personal Codex storage without redirection')
    return selected


def volume_path(path):
    """Check free bytes on the actual target volume even before its first mkdir."""
    while not path.exists():
        if path.parent==path:raise ValueError('CLI private storage volume is unavailable')
        path=path.parent
    return path


def native_root(settings):
    """Selected native root, without opening WSL or guessing an unset path."""
    if not settings.codex_wsl_state_root:return None
    from .codex_wsl_state import native_root as linux_root
    from .runtime_config import get_runtime_setting
    import re
    distro=get_runtime_setting('wsl_distro','vibeflow-linux')
    if not isinstance(distro,str) or not re.fullmatch('[a-z0-9-]+',distro):
        raise ValueError('Invalid private CLI distribution')
    native=str(linux_root(settings.codex_wsl_state_root))
    return Path('\\\\wsl.localhost\\'+distro+native.replace('/','\\'))


def account_directory(path,settings,*,user_id=None):
    """Validate a host or explicitly configured native private account path."""
    from .file_transactions import _path
    folder=Path(path).absolute()
    host=root(settings)
    if folder.is_relative_to(host):
        base=host
    else:
        base=native_root(settings)
        if base is None or not folder.is_relative_to(base):
            raise ValueError('Private vault is outside configured CLI storage')
    relative=folder.relative_to(base)
    if not relative.parts or relative.parts[0]!='users':
        raise ValueError('Private vault requires an account directory')
    if user_id is not None and (type(user_id) is not int or user_id<=0 or len(relative.parts)<2 or relative.parts[1]!=str(user_id)):
        raise ValueError('Private vault belongs to another user')
    selected=_path(base,relative.as_posix())
    if not selected.is_dir():raise ValueError('Private vault account directory is unavailable')
    return selected
