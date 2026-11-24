"""Provision a new deployment key, or restore the original key; never overwrite."""
import argparse
import os
from pathlib import Path

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--copy-from', type=Path, help='Original key required for an existing database')
    args = parser.parse_args()
    key = args.copy_from.read_bytes().strip() if args.copy_from else Fernet.generate_key()
    Fernet(key)  # Validate before creating any destination file.
    args.destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    print('PROVIDER_VAULT_READY '+str(args.destination))


if __name__ == '__main__':
    main()
