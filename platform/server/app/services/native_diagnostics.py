"""Observe one validated native call's append-only local diagnostic file.

This bounds cumulative retained diagnostics, not kernel writes. The helper's
per-call cap remains in force between samples; data outputs use storage quotas.
"""
import stat
from pathlib import Path

from .attempt_workspace import checked_path,ordinary


class DiagnosticMeter:
    def __init__(self,root,config,budget,caller):
        self.root=Path(root)
        self.folder=Path(config).parent
        self.relative=self.folder.relative_to(self.root).as_posix()
        checked_path(self.root,self.relative)
        info=ordinary(self.folder)
        if not stat.S_ISDIR(info.st_mode):raise ValueError('Native diagnostic folder is not a directory')
        self.folder_identity=(info.st_dev,info.st_ino)
        self.file_identity=None
        self.seen=0
        self.budget=budget
        self.source='diagnostic.'+caller

    def sample(self):
        checked_path(self.root,self.relative)
        folder=ordinary(self.folder)
        if (folder.st_dev,folder.st_ino)!=self.folder_identity:
            raise ValueError('Native diagnostic folder was replaced')
        try:
            path=checked_path(self.root,self.relative+'/stderr.bin')
            info=ordinary(path)
        except FileNotFoundError:
            if self.file_identity is not None:raise ValueError('Native diagnostic file disappeared')
            return
        if not stat.S_ISREG(info.st_mode):raise ValueError('Native diagnostic output is not a file')
        identity=(info.st_dev,info.st_ino)
        if self.file_identity is not None and identity!=self.file_identity:
            raise ValueError('Native diagnostic file was replaced')
        if info.st_size<self.seen:raise ValueError('Native diagnostic file was truncated')
        self.file_identity=identity
        delta=info.st_size-self.seen
        if delta:self.budget.consume_output(delta,self.source)
        self.seen=info.st_size

    def checkpoint(self):
        return {'relative':self.relative,'folder_identity':list(self.folder_identity),
            'file_identity':list(self.file_identity) if self.file_identity is not None else None,'seen':self.seen}
