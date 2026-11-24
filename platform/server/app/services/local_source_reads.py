"""Replay filesystem identity checks after a professional source read."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import stat

from .. import storage
from ..storage_paths import contains_file_stage
from . import attempt_workspace as workspace
from .case_provision import io
from .case_write import CaseWriteConflict
from .local_file_edits import _version
from .storage_listing_budget import ListingBudget


_active=ContextVar('local_source_reads',default=None)
# These validators read all live files through their declared case views.
SCOPED={'geometry.preview','geometry.export','mesh.generate','solve.openfoam','solve.openfoam.resume',
        'pdf.number','ppt.build','postprocess.run','postprocess.case','moose.solve','code-aster.solve'}
# These contracts discover their dependencies within the complete public tree.
WHOLE={'calculix.solve','elmer.solve','flotherm.convert','flotherm.build-library','latex.build','mpi.openfoam',
       'paraview.run','report.build','report.compose','report.compose-generated','solve.openfoam.mpi',
       'solve.openfoam.prepared','solve.openfoam.udf','su2.solve','udf.build','udf.build.workflow','udf.integrate'}


def stale():
    return CaseWriteConflict('input_stale','专业来源文件在读取或提交前发生变化')


@dataclass(frozen=True)
class Selection:
    root: Path
    paths: tuple[str,...] | None

    def snapshot(self):
        try:
            workspace.ordinary(self.root)
            budget=ListingBudget()
            paths=workspace.live_roots(self.root) if self.paths is None else self.paths
            found={}

            def visit(path):
                budget.visit()
                relative=workspace.canonical_path(path.relative_to(self.root).as_posix())
                if relative in found or contains_file_stage(relative):
                    return
                if stat.S_ISDIR(workspace.ordinary(path).st_mode):
                    found[relative]=None
                    for child in path.iterdir():
                        visit(child)
                else:
                    found[relative]=_version(path)
                    if found[relative] is None:
                        raise stale()

            for name in paths:
                try:
                    selected=workspace.checked_path(self.root,name)
                except FileNotFoundError:
                    continue
                visit(selected)
            return found
        except CaseWriteConflict:
            raise
        except (OSError,ValueError) as error:
            raise stale() from error


class LocalSourceReads:
    def __init__(self):
        self.records={}
        self.uncovered=set()
        self.sealed=False
        self.observations=0

    @contextmanager
    def collect(self):
        if storage._use_s3():
            yield
            return
        token=_active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    @contextmanager
    def observe(self,selection):
        if self.sealed:
            raise ValueError('Professional source reads are already sealed')
        before=selection.snapshot()
        if selection in self.records and self.records[selection]!=before:
            raise stale()
        yield
        if selection.snapshot()!=before or self.records.setdefault(selection,before)!=before:
            raise stale()
        self.observations+=1

    def seal(self):
        self.sealed=True

    def _require_versions(self):
        if not self.sealed:
            raise ValueError('Professional source reads were not sealed')
        for selection,expected in self.records.items():
            if selection.snapshot()!=expected:
                raise stale()

    async def require_current(self):
        await io(self._require_versions)


def observe(root,paths):
    reads=_active.get()
    if reads is None or reads.sealed:
        return nullcontext()
    selection=None if paths is None else tuple(sorted({workspace.canonical_path(name) for name in paths}))
    return reads.observe(Selection(root,selection))


def supported(handler):
    return handler.key in SCOPED or handler.key in WHOLE or handler.key.startswith('command.')


def read_source(handler,prefix,descriptor):
    reads=_active.get()
    if reads is None or reads.sealed:
        return handler.source(prefix,descriptor)
    if handler.key in WHOLE or handler.key.startswith('command.'):
        with observe(storage._fs_path(prefix,'.'),None):
            return handler.source(prefix,descriptor)
    if not supported(handler):
        reads.uncovered.add(handler.key)
        return handler.source(prefix,descriptor)
    before=reads.observations
    result=handler.source(prefix,descriptor)
    if reads.observations==before:
        raise ValueError('Registered source inspection did not record its filesystem view')
    return result
