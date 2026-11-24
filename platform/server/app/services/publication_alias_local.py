"""Prepare local publication copies before taking the case write transaction."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path

from .. import storage
from . import attempt_workspace as workspace, case_copy_io, file_transactions as files
from . import publication_alias_journal as aliases, publication_journal as journal
from .case_provision import io
from .case_read_capacity import file_read_deadline
from .file_stage_locations import batches, locate
from .local_file_budget import check_quota, replacement_peak
from .local_file_edits import _version
from .request_deadline import STAGING_CLEANUP_SECONDS, current


SCHEMA = 'vibeflow.local-publication-alias-staging.v1'


@dataclass(frozen=True)
class Member:
    operation: dict
    version: tuple | None
    source: Path | None
    action: str


def describe(prefix, row, digest):
    from .artifact_publication import read_manifest

    root, directory = journal._directory(prefix, row)
    with journal._lock(directory):
        value = aliases.read_plan(directory, row, digest)
    archive, manifest = read_manifest(prefix, row)
    scratch = storage._fs_path('.', '.publication-alias-staging')
    members, objects, replacements = [], {}, []
    for index, operation in enumerate(value['operations']):
        relative = workspace.canonical_path(operation['path'])
        workspace.assert_mutable_path(prefix, relative)
        target = files._path(root, relative)
        version = _version(target)
        before, after = operation['before'], operation['after']
        present = None if version is None else files._capture(target, limit=version[2], deadline=current())
        if _version(target) != version:
            raise ValueError('Compatibility destination changed: '+relative)
        source = None
        if present == after:
            action = 'keep'
        elif present != before:
            if not aliases.preserves_retired_edit(operation):
                raise ValueError('Edited compatibility destination was preserved: '+relative)
            action = 'preserve'
        elif operation['source'] is None:
            action = 'remove'
            replacements.append((before, None))
        else:
            record = manifest['files'][operation['source']]
            if {key: record[key] for key in ('size', 'sha256')} != after:
                raise ValueError('Alias source no longer matches its publication')
            source = workspace.checked_path(archive/'files', operation['source'])
            objects[index] = (locate(root, relative, scratch), after['size'])
            replacements.append((before, after))
            action = 'install'
        members.append(Member(operation, version, source, action))
    return root, directory, members, objects, replacement_peak(replacements, 0)


def copy_replacements(members, objects, directories):
    copied = {}
    for index, (location, size) in objects.items():
        member = members[index]
        source_version = _version(member.source)
        target = directories[location]/str(index)
        observed = files._capture(member.source, target, limit=size, deadline=current())
        if observed != member.operation['after'] or _version(member.source) != source_version:
            raise ValueError('Alias source changed during preparation')
        copied[index] = (target, _version(target), source_version)
    return copied


@dataclass(frozen=True)
class PreparedAliases:
    root: Path
    directory: Path
    digest: str
    members: list[Member]
    copied: dict
    peak: int

    async def publish(self, db, case, row):
        if storage._fs_path(case.s3_prefix, '.') != self.root:
            raise ValueError('Alias case source changed during preparation')
        if self.peak:
            await check_quota(db, case, self.peak)
        await io(self.install, case.s3_prefix, row)

    def install(self, prefix, row):
        with journal._lock(self.directory):
            value = aliases.read_plan(self.directory, row, self.digest)
            if value['operations'] != [member.operation for member in self.members]:
                raise ValueError('Alias plan changed during preparation')
            preserved = set()
            for index, member in enumerate(self.members):
                operation = member.operation
                relative = operation['path']
                workspace.assert_mutable_path(prefix, relative)
                if member.action == 'preserve' or _version(files._path(self.root, relative)) != member.version:
                    if not aliases.preserves_retired_edit(operation):
                        raise ValueError('Compatibility destination changed: '+relative)
                    preserved.add(relative)
                if member.action == 'install':
                    replacement, version, source = self.copied[index]
                    if _version(replacement) != version or _version(member.source) != source:
                        raise ValueError('Prepared alias source changed: '+relative)
            for index, member in enumerate(self.members):
                relative = member.operation['path']
                if relative in preserved or member.action == 'keep':
                    continue
                target = files._path(self.root, relative)
                if _version(target) != member.version:
                    if aliases.preserves_retired_edit(member.operation):
                        preserved.add(relative)
                        continue
                    raise ValueError('Compatibility destination changed: '+relative)
                if member.action == 'remove':
                    target.unlink()
                else:
                    replacement, version, source = self.copied[index]
                    if _version(replacement) != version or _version(member.source) != source:
                        raise ValueError('Prepared alias source changed: '+relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(replacement, target)
                files._sync(target.parent)
            if preserved:
                raise ValueError('Edited retired solve outputs were preserved: '+', '.join(sorted(preserved)))
            aliases.record_applied(self.directory, self.digest)


@asynccontextmanager
async def stage(prefix, row, digest):
    if not digest or storage._use_s3():
        yield None
        return
    async with file_read_deadline('publication_alias','应用发布兼容文件',seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code='publication_alias_busy', label='准备发布兼容文件'):
            root,directory,members,objects,peak = await io(describe, prefix, row, digest)
            async with batches(objects, SCHEMA) as directories:
                copied = await io(copy_replacements, members, objects, directories)
                yield PreparedAliases(root, directory, digest, members, copied, peak)
