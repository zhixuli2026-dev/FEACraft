"""Validated image uploads and immutable local inputs for the native CLI.

The HTTP boundary accepts bytes, never an arbitrary local path or remote URL.
Public history references case storage; CLI history references a private copy
which survives the owned session for explicit thread resume.
"""
import hashlib
import os
from pathlib import Path

from .. import storage
from .owned_process import wsl_path

EXTENSIONS = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp'}
DOCKER_INPUTS = '/inputs'


def docker_input_root(auth_home, case_id):
    # A stable mount preserves localImage paths across resume and fork. The
    # private parent is never mounted, so other cases remain inaccessible.
    private = Path(auth_home).parent / 'case-inputs'
    if private.resolve() != private:
        raise ValueError('Docker input parent was redirected')
    private.mkdir(mode=0o700, exist_ok=True)
    root = private / str(case_id)
    if root.resolve() != root:
        raise ValueError('Docker case input directory was redirected')
    root.mkdir(mode=0o755, exist_ok=True)
    if os.name == 'posix':
        private.chmod(0o700)
        root.chmod(0o755)
    return root


def prepare(images):
    from ..routers.agent import _prepare_message_images
    prepared = _prepare_message_images(images)
    for metadata, _ in prepared:
        # Keep the display filename out of storage keys, which must remain
        # stable even when public projection redacts sensitive filename text.
        metadata['path'] = metadata['path'].rsplit('/', 1)[0] + '/image' + EXTENSIONS[metadata['mime']]
    return prepared


def fingerprint(prepared):
    return [{'name': metadata['name'], 'mime': metadata['mime'], 'size': len(content),
             'sha256': hashlib.sha256(content).hexdigest()} for metadata, content in prepared]


class Publication:
    def __init__(self, session, turn_id, prepared):
        self.session, self.prepared = session, prepared
        self.folder = session.input_root / turn_id
        self.files, self.written, self.metadata = [], [], []
        self.committing = False
        self.rolled_back = False

    def write(self):
        if not self.prepared:
            return
        from ..config import get_settings
        from .cli_private_paths import root as private_root
        from .node_storage_budget import reserve
        if not self.folder.is_relative_to(private_root(get_settings())):
            raise ValueError('Native image input escaped configured private state')
        copies=1 if storage._use_s3() else 2
        with reserve(copies*sum(len(data) for _,data in self.prepared),private=True):
            self._write()

    def _write(self):
        root = self.session.input_root
        if root.is_symlink() or root.resolve() != root or root.parent.resolve() != root.parent:
            raise ValueError('Native input directory changed')
        root.mkdir(mode=0o700, exist_ok=True)
        docker = self.session.runtime == 'docker'
        self.folder.mkdir(mode=0o755 if docker else 0o700, exist_ok=False)
        if docker and os.name == 'posix':
            self.folder.chmod(0o755)
        for index, (metadata, content) in enumerate(self.prepared):
            path = self.folder / (str(index) + EXTENSIONS[metadata['mime']])
            with path.open('xb') as stream:
                stream.write(content)
                if docker and os.name == 'posix':
                    os.fchmod(stream.fileno(), 0o644)
            digest = hashlib.sha256(content).hexdigest()
            self.files.append((path, digest, len(content)))
            self.written.append(metadata['path'])
            storage.put_bytes(self.session.selected['source_prefix'], metadata['path'], content, metadata['mime'])
            self.metadata.append({**metadata, 'sha256': digest})

    def verify(self):
        if self.files and (self.folder.is_symlink() or self.session.input_root.is_symlink()
                or self.folder.resolve() != self.folder or self.session.input_root.resolve() != self.session.input_root):
            raise ValueError('Native input directory changed')
        for path, expected, size in self.files:
            if path.is_symlink() or path.resolve() != path or path.stat().st_size != size:
                raise ValueError('Native image input changed')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise ValueError('Native image input changed')

    def paths(self):
        if self.session.runtime == 'docker':
            return [DOCKER_INPUTS + '/' + path.relative_to(self.session.input_root).as_posix()
                    for path, _, _ in self.files]
        return [wsl_path(path) if self.session.runtime == 'wsl' else str(path) for path, _, _ in self.files]

    def rollback(self):
        # After commit begins its acknowledgement may be lost. Retain both
        # copies for reconciliation rather than breaking a committed message.
        if self.committing or self.rolled_back:
            return
        self.rolled_back = True
        for name in self.written:
            try: storage.delete(self.session.selected['source_prefix'], name)
            except Exception: pass
        for path, _, _ in self.files:
            try: path.unlink(missing_ok=True)
            except OSError: pass
        try: self.folder.rmdir()
        except OSError: pass
