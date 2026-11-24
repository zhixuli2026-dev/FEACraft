"""Load deployment keys and initialize the process account's local provider vault."""

import os
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet


DPAPI_PREFIX = b'vibeflow-dpapi-v1:'


def read(path: Path) -> Fernet:
    key = path.read_bytes().strip()
    if key.startswith(DPAPI_PREFIX):
        from .scnet import dpapi_unprotect
        key = dpapi_unprotect(key[len(DPAPI_PREFIX):].decode('ascii'))
    return Fernet(key)


def local(path: Path) -> Fernet:
    try:
        return read(path)
    except FileNotFoundError:
        pass

    key = Fernet.generate_key()
    if os.name == 'nt':
        from .scnet import dpapi_protect
        key = DPAPI_PREFIX + dpapi_protect(key).encode('ascii')
    # Publish completed bytes in one filesystem operation. If another worker
    # initialized the vault first, keep its key and discard the staged file.
    descriptor, name = tempfile.mkstemp(prefix='.provider-vault-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return read(path)
