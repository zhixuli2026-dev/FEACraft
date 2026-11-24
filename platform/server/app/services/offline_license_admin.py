"""Issue VibeFlow offline grants using an encrypted, operator-held Ed25519 key."""
from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import os
import sys
import time
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import offline_license as license


def write_new(path, content, *, private=False):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    with os.fdopen(descriptor,'wb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def public_pem(key):
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def password(args, *, confirm=False):
    if args.passphrase_file is not None:
        value = license.read_file(args.passphrase_file,1024).rstrip(b'\r\n')
    else:
        value = getpass.getpass('Issuer key passphrase: ').encode('utf8')
        if confirm and getpass.getpass('Confirm issuer key passphrase: ').encode('utf8') != value:
            raise ValueError('Passphrases do not match')
    if not 12 <= len(value) <= 1024:
        raise ValueError('Issuer passphrase must have at least 12 bytes')
    return value


def signed_bundle(key, grant, revocations):
    return license.canonical({'grant':license.sign(license.GRANT, grant, key),
                              'revocations':license.sign(license.REVOCATIONS, revocations, key)}) + b'\n'


def issue(key, *, deployment, org_id, plan, device, days, previous=None, at=None):
    now = int(time.time()) if at is None else at
    if not 1 <= days <= 3660:
        raise ValueError('License validity must be between 1 and 3660 days')
    if previous is None:
        revocations = license.Revocations(deployment=deployment, sequence=1, issued_at=now)
    else:
        old, revoked = license.validated_bundle(previous, public_pem(key))
        if old.deployment != deployment or old.org_id != org_id:
            raise ValueError('Replacement must retain the deployment and organization')
        serials = sorted(set(revoked.serials) | {old.serial})
        revocations = license.Revocations(deployment=deployment, sequence=revoked.sequence+1,
                                          issued_at=now, serials=serials)
    grant = license.Grant(serial=uuid4(), deployment=deployment, org_id=org_id, plan=plan,
        device=device, issued_at=now, not_before=now, expires_at=now+days*86400, revocation_sequence=revocations.sequence)
    return signed_bundle(key, grant, revocations)


def revoke(key, previous, *, at=None):
    grant, prior = license.validated_bundle(previous, public_pem(key))
    if grant.serial in prior.serials:
        return previous
    revocations = license.Revocations(deployment=prior.deployment, sequence=prior.sequence+1,
        issued_at=int(time.time()) if at is None else at, serials=sorted(set(prior.serials) | {grant.serial}))
    return signed_bundle(key, grant, revocations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('device', help='Print this installation’s application-specific device fingerprint')
    keygen = commands.add_parser('generate-key')
    keygen.add_argument('--public-key', type=Path, required=True)
    issue_cmd = commands.add_parser('issue')
    issue_cmd.add_argument('--deployment', type=UUID, required=True)
    issue_cmd.add_argument('--org-id', type=int, required=True)
    issue_cmd.add_argument('--plan', choices=['pro','team'], required=True)
    issue_cmd.add_argument('--device', required=True)
    issue_cmd.add_argument('--days', type=int, required=True)
    issue_cmd.add_argument('--previous', type=Path, help='Prior signed bundle to renew or transfer')
    revoke_cmd = commands.add_parser('revoke')
    revoke_cmd.add_argument('--previous', type=Path, required=True)
    for command in (keygen, issue_cmd, revoke_cmd):
        command.add_argument('--private-key', type=Path, required=True)
        command.add_argument('--passphrase-file', type=Path)
    for command in (issue_cmd, revoke_cmd):
        command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'device':
        print(license.device())
        return
    if args.command == 'generate-key':
        if args.private_key.resolve() == args.public_key.resolve() or args.private_key.exists() or args.public_key.exists():
            parser.error('Choose two unused issuer key paths')
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.BestAvailableEncryption(password(args, confirm=True)))
        write_new(args.private_key, pem, private=True)
        write_new(args.public_key, public_pem(key))
        return
    key = serialization.load_pem_private_key(license.read_file(args.private_key,4096), password(args))
    if not isinstance(key, Ed25519PrivateKey):
        parser.error('Expected an Ed25519 issuer private key')
    previous = license.read_file(args.previous) if args.previous else None
    if args.command == 'issue':
        output = issue(key, deployment=args.deployment, org_id=args.org_id, plan=args.plan,
                       device=args.device, days=args.days, previous=previous)
    else:
        output = revoke(key, previous)
    write_new(args.output, output)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(type(error).__name__ + ': offline license operation failed', file=sys.stderr)
        sys.exit(1)
