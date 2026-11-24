"""Directory-preserving live selections around pinned publication files."""
from collections import defaultdict
from pathlib import PurePosixPath

from .attempt_workspace import canonical_path
from .workflow_state import ExecutionConflict


def overlay_snapshot(source,publications):
    """Bind a native inventory to verified publication bytes, never aliases.

    This only builds metadata. The ordinary seal still mounts each immutable
    member, rechecks its bytes and applies all inventory/quota/race rules.
    """
    from . import attempt_workspace as workspace
    files=dict(source['files']);directories=set(source['directories']);seen=set()
    for publication in publications:
        for path,record in publication.files.items():
            if path in seen or path in directories:
                raise ExecutionConflict('input_stale','A fixed publication file collides with another input or live directory')
            seen.add(path)
            actual=workspace.digest_file(workspace.checked_path(publication.root,path))
            if any(actual[k]!=record[k] for k in ('size','sha256')):
                raise ExecutionConflict('input_stale','Fixed publication input changed during native preparation')
            files[path]=dict(record)
            directories.update(p.as_posix() for p in PurePosixPath(path).parents if p.as_posix()!='.')
    if set(files)&directories or len(files)>workspace.MAX_FILES or sum(r['size'] for r in files.values())>workspace.MAX_BYTES:
        raise ExecutionConflict('input_stale','Composite native input inventory is invalid or exceeds its limit')
    return {**source,'files':dict(sorted(files.items())),'directories':sorted(directories)}


def live_selection(source, mounted_files):
    """Select every original live file once, except explicitly mounted files.

    Descend only into ancestors of mounted files. Unaffected directories stay
    directory selections, including empty directories. Snapshot/seal still
    performs all live inventory, symlink, byte and concurrent-change checks.
    """
    files=set(source['files']);directories=set(source['directories'])
    mounted=set(mounted_files)
    if not mounted<=files or files&directories:
        raise ExecutionConflict('input_stale','Composite publication files do not match the observed live inventory')
    for name in files|directories|mounted:
        if canonical_path(name)!=name or name.split('/')[0].casefold()=='.vibeflow':
            raise ValueError('Composite inputs require canonical user-file paths')
    blocked=set()
    for name in mounted:
        blocked.update(parent.as_posix() for parent in PurePosixPath(name).parents)
    nodes=files|directories
    for name in list(nodes):
        nodes.update(p.as_posix() for p in PurePosixPath(name).parents if p.as_posix()!='.')
    children=defaultdict(list)
    for name in nodes:children[PurePosixPath(name).parent.as_posix()].append(name)
    selected=[]
    def visit(name):
        if name in mounted:return
        if name not in blocked:
            selected.append(name)
            return
        for child in sorted(children[name]):visit(child)
    for name in sorted(children['.']):visit(name)
    return sorted(selected)
