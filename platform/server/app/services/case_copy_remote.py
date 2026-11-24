"""Stage authenticated object copies before assigning published case IDs."""
from dataclasses import dataclass

from . import case_provision
from .case_events import CASE_EVENTS_PATH
from .case_source import SourceChanged, remote_versions, verify_source
from .casedict import CASE_DICT_PATH
from .provenance import PROVENANCE_PATH


@dataclass(frozen=True)
class RemoteCopy:
    draft: dict
    versions: dict
    metadata: dict[str, bytes]

    async def publish(self, db, case, final, rendered):
        prefix = self.draft['binding']['prefix']
        if await case_provision.io(remote_versions, prefix) != self.versions:
            raise SourceChanged('提交前远端复制暂存已变化，请重试')
        plan = await case_provision.io(case_provision.bind_remote_copy, self.draft,
                                       case.id, case.workspace_id, final)
        case.s3_prefix = prefix
        versions = self.versions.copy()
        for path, data in rendered.items():
            await case_provision.io(case_provision.write_file, plan, path, data)
            observed, version = await case_provision.io(case_provision.snapshot, prefix, path)
            if observed != final[path] or not version:
                raise case_provision.conflict('新算例内容与创建记录不一致')
            versions[path] = (observed['size'], version.strip('"'))
        if await case_provision.io(remote_versions, prefix) != versions:
            raise SourceChanged('提交前远端复制暂存已变化，请重试')
        await case_provision.record_witness(db, case, plan)


def stage(user_id, source_id, manifest, kind, exclude):
    rendered_paths = (CASE_DICT_PATH, CASE_EVENTS_PATH, PROVENANCE_PATH)
    metadata = {path: manifest[path].read_content() for path in rendered_paths if path in manifest}
    files = {path: entry.record for path, entry in manifest.items() if path not in rendered_paths}
    draft = case_provision.prepare_remote_copy(user_id, source_id, files, kind)
    prepared = stage_draft(draft, manifest, metadata)
    verify_source(manifest[CASE_DICT_PATH].prefix, manifest, exclude=exclude)
    return prepared


def stage_draft(draft, manifest, metadata):
    for path in draft['files']:
        case_provision.write_file(draft, path, manifest[path])
    prefix = draft['binding']['prefix']
    versions = remote_versions(prefix)
    case_provision.verify_files(draft)
    if remote_versions(prefix) != versions:
        raise SourceChanged('远端复制暂存已变化，请重试')
    return RemoteCopy(draft, versions, metadata)
