"""Prepare local constructor payloads outside the source and quota transactions."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import stat

from .. import storage
from . import case_provision, node_storage_budget, owned_staging
from .attempt_workspace import ordinary
from .case_events import CASE_EVENTS_PATH
from .case_source import SourceChanged, verify_source
from .casedict import CASE_DICT_PATH
from .file_stage_locations import MOUNTED_SCHEMA, existing_parent, locate
from .file_transactions import _capture, _path, _sync, _write_new
from .fs_directory import move_new
from .local_file_edits import _version
from .provenance import PROVENANCE_PATH
from .request_deadline import current


RENDERED = (CASE_DICT_PATH, CASE_EVENTS_PATH, PROVENANCE_PATH)
STAGE_SCHEMA = 'vibeflow.local-case-copy-stage.v1'


def case_destination(user_id):
    return f'workspaces/{user_id}/cases/.constructor-target'


def staging_location(user_id, default):
    root = storage._fs_root().resolve()
    return locate(root, case_destination(user_id), _path(root, f'{default}/{user_id}'))


def staging_batch(location, size, schema):
    return owned_staging.batch(location.scratch, size, MOUNTED_SCHEMA if location.local else schema,
                               target_root=location.scratch if location.local else None)


@dataclass(frozen=True)
class LocalCopy:
    directory: Path
    versions: dict
    metadata: dict[str, bytes]

    def accounted_bytes(self, user_id):
        owner = storage._fs_root().resolve() / 'workspaces' / str(user_id)
        if self.directory.is_relative_to(owner):
            return sum(version[2] for version in self.versions.values())
        return 0

    def install(self, plan, rendered):
        root = storage._fs_root().resolve()
        target = _path(root, plan['binding']['prefix'])
        if ordinary(self.directory).st_dev != ordinary(existing_parent(target)).st_dev:
            raise SourceChanged('算例存储挂载已变化，请重试')
        observed = {}
        for path in self.directory.rglob('*'):
            case_provision.check_deadline()
            if stat.S_ISREG(ordinary(path).st_mode):
                observed[path.relative_to(self.directory).as_posix()] = _version(path)
        if observed != self.versions:
            raise SourceChanged('安装前复制暂存已变化，请重试')

        for path, data in rendered.items():
            case_provision.check_deadline()
            _write_new(self.directory / path, data)
        case_provision.check_deadline()
        move_new(self.directory, target, root=root)
        _sync(target.parent)

    async def publish(self, db, case, user_id, manifest, rendered, kind):
        from .local_provision_budget import hold

        async with hold(db, user_id):
            plan = await case_provision.io(case_provision.prepare, case.id, case.workspace_id,
                                           user_id, manifest, kind)
            case.s3_prefix = plan['binding']['prefix']
            if rendered:
                additional = sum(len(value) for value in rendered.values())
                async with node_storage_budget.allocation(additional, target_root=self.directory):
                    await case_provision.io(self.install, plan, rendered)
            else:
                await case_provision.io(self.install, plan, rendered)
            await case_provision.record_witness(db, case, plan)


def _stage_case(directory, manifest, exclude):
    directory.mkdir(exist_ok=False)
    metadata = {path: manifest[path].read_content() for path in RENDERED if path in manifest}
    versions = {}
    for path, source in manifest.items():
        if path in RENDERED:
            continue
        case_provision.check_deadline()
        target = directory / path
        source.copy_into(target)
        _sync(target.parent)
        before = _version(target)
        if (_capture(target, limit=source.size, deadline=current()) != source.record
                or _version(target) != before):
            raise SourceChanged('复制暂存未通过正文校验，请重试')
        versions[path] = before
    prefix = next(iter(manifest.values())).prefix
    verify_source(prefix, manifest, exclude=exclude)
    return LocalCopy(directory, versions, metadata)


@asynccontextmanager
async def prepare(location, manifests, exclude):
    versions = {}
    total = sum(record.size for manifest in manifests.values() for path, record in manifest.items()
                if path not in RENDERED)
    async with staging_batch(location, total, STAGE_SCHEMA) as directory:
        for case_id, manifest in manifests.items():
            if CASE_DICT_PATH not in manifest:
                from .case_admin import CaseAdminError
                raise CaseAdminError('源算例缺少 caseDict，无法复制', 409)
            versions[case_id] = await case_provision.io(_stage_case, directory / str(case_id), manifest, exclude)
        yield versions
