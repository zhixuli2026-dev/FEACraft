"""Recover interrupted local seal-journal installation, including its hard link."""
import json
import os
from pathlib import PurePosixPath
import re
import stat
import uuid

from .. import storage
from . import file_transactions as journal, process_identity_native as identity
from . import attempt_workspace as workspace
from .durable_events import json_text

SCHEMA = 'vibeflow.attempt-plan-write.v1'
MAX_OWNER_BYTES = 1024


def put(prefix, path, data):
    from . import attempt_seal_journal as seals
    target = seals.local(prefix, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    base = seals.PREFIX + '/writing/' + identifier
    part, intent = seals.local(prefix, base + '.part'), seals.local(prefix, base + '.json')
    record = {'schema': SCHEMA, 'id': identifier, 'binding': seals.binding(prefix), 'target': path,
              'owner': identity.observe(os.getpid()), 'file': seals.objects.digest(data)}
    if len(json_text(record['owner']).encode('utf8')) > MAX_OWNER_BYTES:
        raise ValueError('Seal journal owner exceeds its admitted metadata bound')
    try:
        journal._write_new(intent, json_text(record).encode('utf8'))
        journal._write_new(part, data)
        seals.local(prefix, path)
        os.link(part, target)
        journal._sync(target.parent)
    finally:
        part.unlink(missing_ok=True)
        intent.unlink(missing_ok=True)


def _leaf(prefix, relative):
    """Allow a two-link leaf only until its exact installation pair is checked."""
    from . import attempt_seal_journal as seals
    parts = PurePosixPath(relative)
    path = seals.local(prefix, parts.parent.as_posix()) / parts.name
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or
            info.st_nlink not in {1, 2} or info.st_size > seals.MAX_RECORD):
        raise ValueError('Unrecognized journal staging file')
    return path, info


def inspect(prefix, part_name, record):
    try:
        part, info = _leaf(prefix, part_name)
    except FileNotFoundError:
        return None
    if info.st_nlink == 2:
        target, target_info = _leaf(prefix, record['target'])
        if target_info.st_nlink != 2 or not os.path.samefile(part, target):
            raise ValueError('Journal staging hard link differs from its target')
    from .request_deadline import current
    if workspace.digest_file(part, deadline=current()) != record['file']:
        raise ValueError('Journal staging file is partial or changed')
    return part


def recover(prefix, *, apply=True, selected=None):
    from . import attempt_seal_journal as seals
    if storage._use_s3():
        return []
    parent = seals.local(prefix, seals.PREFIX + '/writing')
    if not parent.exists():
        return []
    outcomes, handled = [], set()
    if selected is None:
        names = {path.name for path in parent.iterdir()}
    else:
        # The writing page groups the intent and payload under the same name.
        names = set()
        for name in selected:
            related = (name, name.removesuffix('.json') + '.part') if name.endswith('.json') else (name,)
            names.update(item for item in related if os.path.lexists(parent / item))
    from .case_provision import check_deadline
    for name in sorted(names):
        check_deadline()
        if not re.fullmatch('[0-9a-f]{32}\.json', name):
            continue
        handled.update({name, name.removesuffix('.json') + '.part'})
        row = {'record': seals.PREFIX + '/writing/' + name}
        try:
            raw, _ = seals.read(prefix, row['record'])
            record = json.loads(raw)
            target = record['target']
            if (record['schema'] != SCHEMA or record['id'] != name[:-5] or
                    record['binding'] != seals.binding(prefix) or not re.fullmatch(
                        re.escape(seals.PREFIX) + r'/(pending|history)/[0-9a-f-]{36}\.json', target)):
                raise ValueError('Journal staging identity changed')
            identifier = target.rsplit('/', 1)[1][:-5]
            if str(uuid.UUID(identifier)) != identifier:
                raise ValueError('Invalid journal staging target')
            expected = record['file']
            if (type(expected['size']) is not int or not 0 <= expected['size'] <= seals.MAX_RECORD or
                    not re.fullmatch('[0-9a-f]{64}', expected['sha256'])):
                raise ValueError('Invalid journal staging digest')
            observed = identity.probe(record['owner'])
            if observed['state'] != 'exited':
                row.update(status='active_writer' if observed['state'] == 'match' else 'unverified_writer_retained')
            else:
                part_name = seals.PREFIX + '/writing/' + name[:-5] + '.part'
                inspect(prefix, part_name, record)
                row['status'] = 'journal_write_pending'
                if apply:
                    if seals.read(prefix, row['record'])[0] != raw:
                        raise ValueError('Journal staging intent changed')
                    part = inspect(prefix, part_name, record)
                    check_deadline()
                    if part is not None:
                        part.unlink()
                    seals.local(prefix, row['record']).unlink()
                    row['status'] = 'journal_write_removed'
        except TimeoutError:
            raise
        except Exception as error:
            row.update(status='recovery_required', reason=str(error))
        outcomes.append(row)
    outcomes.extend({'record': seals.PREFIX + '/writing/' + name, 'status': 'untracked_writing_retained'}
                    for name in sorted(names - handled))
    return outcomes
