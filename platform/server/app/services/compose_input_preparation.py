"""Prepare report drafts without a case write lock, then bind the same inputs."""
import codecs
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json

from .. import storage
from . import attempt_workspace, compose_artifacts
from .case_read_view import remote_file_versions, selected_view
from .case_write import CaseWriteConflict
from .casedict import CASE_DICT_PATH, parse_casedict
from .file_read import FileRead, FileReadConflict


_POINTER = 'D-post/data/latest-postprocess.json'
_MESH = 'B-mesh/quality.json'


def _stale():
    return CaseWriteConflict('input_stale', '报告草稿准备期间输入已变化，请重试')


@dataclass(frozen=True)
class DraftInputs:
    prefix: str
    run_dir: str
    source: dict
    dictionary: dict
    output: str
    previous: dict | None
    has_text: bool
    versions: dict
    remote: bool

    def describe(self, registered):
        if not registered:
            return self.source
        # The registered publication already mounts its own evidence. Only
        # the independently selected figures and template files belong here.
        records = self.source['files']
        resources = {item['selected']:records[item['selected']] for item in self.source['resource_resolution']}
        return {**self.source, 'files':resources}

    def verify_versions(self, prefix):
        if self.prefix!=prefix or self.remote!=storage._use_s3():
            raise _stale()
        if self.remote:
            observed = remote_file_versions(prefix,self.versions)
        else:
            observed = {path:_local_revision(prefix,path) for path in self.versions}
        if observed!=self.versions:
            raise _stale()

    def verify_seal(self, attempt, prepared):
        manifest = json.loads(attempt.input_manifest_json)['workspace']
        self.verify_manifest(manifest,prepared)

    def verify_manifest(self, manifest, prepared):
        records = {CASE_DICT_PATH:self.dictionary, **prepared.descriptor['source']['files']}
        if self.previous is not None:
            records[self.output] = self.previous
        for path, expected in records.items():
            actual = manifest['files'].get(path)
            if actual is None or any(actual[key]!=expected[key] for key in ('size','sha256')):
                raise _stale()


def _local_revision(prefix,path):
    try:
        return FileRead(prefix,path).version
    except FileNotFoundError:
        return None


def _verify_local(prefix, records, paths):
    versions = {}
    for path in paths:
        expected = records.get(path)
        version = None
        if expected is None:
            if _local_revision(prefix,path) is not None:
                raise _stale()
        else:
            try:
                reader = FileRead(prefix,path)
                observed = hashlib.sha256()
                with closing(reader.chunks()) as blocks:
                    for chunk in blocks:
                        observed.update(chunk)
                version = reader.version
                if reader.size!=expected['size'] or observed.hexdigest()!=expected['sha256'] or _local_revision(prefix,path)!=version:
                    raise _stale()
            except (FileNotFoundError, FileReadConflict) as error:
                raise _stale() from error
        versions[path] = version
    return versions


def _read_previous(root, path, overwrite):
    target = compose_artifacts.existing_path(root,path)
    if target is None:
        return None, False
    from .request_deadline import current
    deadline = current()
    decoder = None if overwrite else codecs.getincrementaldecoder('utf-8')(errors='replace')
    digest, size, has_text = hashlib.sha256(), 0, False
    with target.open('rb') as stream:
        for block in iter(lambda:stream.read(64*1024), b''):
            if deadline is not None:
                deadline.check()
            digest.update(block)
            size += len(block)
            if decoder is not None and not has_text:
                has_text = bool(decoder.decode(block).strip())
        if decoder is not None and not has_text:
            has_text = bool(decoder.decode(b'',final=True).strip())
    return {'size':size,'sha256':digest.hexdigest()}, has_text


@contextmanager
def _draft_view(prefix, *, standalone, output=None):
    if not storage._use_s3():
        yield storage._fs_path(prefix,'.'), None, None
        return
    with selected_view(prefix) as selected:
        if not selected.load(CASE_DICT_PATH):
            raise ValueError('报告算例缺少 caseDict')
        if standalone:
            selected.load(_MESH)
        if output is not None:
            selected.load(output)
        yield selected.root, selected.load, selected.versions


def inspect_live_source(prefix, *, standalone, track_resolution, output=None, overwrite=False):
    with _draft_view(prefix, standalone=standalone, output=output) as (root, load, _):
        source = compose_artifacts.source(root, standalone=standalone, gate_prefix=prefix,
                                          track_resolution=track_resolution, load=load)
        previous, has_text = _read_previous(root, output, overwrite) if output is not None else (None, False)
        return source, previous, has_text


def capture(prefix, output, overwrite):
    from .postprocess_executor import config_from_case_dict

    remote = storage._use_s3()
    with _draft_view(prefix,standalone=True,output=output) as (root, load, observed):
        dictionary = attempt_workspace.checked_path(root,CASE_DICT_PATH).read_bytes()
        run_dir = config_from_case_dict(parse_casedict(dictionary.decode('utf-8')))['run_dir']
        source = compose_artifacts.source(root, standalone=True, gate_prefix=prefix,
                                         validated_gate=True, track_resolution=True, load=load)
        previous, has_text = _read_previous(root,output,overwrite)
    dictionary = compose_artifacts.record(dictionary)
    records = {CASE_DICT_PATH:dictionary, **source['files']}
    guard = source['postprocess_guard']
    evidence = dict(guard['evidence'])
    if guard['postprocess_pointer_sha256'] is not None:
        evidence[_POINTER] = guard['postprocess_pointer_sha256']
    if any(records.get(path,{}).get('sha256')!=digest for path,digest in evidence.items()):
        raise _stale()
    if previous is not None:
        records[output] = previous
    if remote:
        versions = observed
        if any(versions.get(path) is None or versions[path]['size']!=record['size'] for path,record in records.items()):
            raise _stale()
    else:
        paths = sorted({*records, output, _POINTER, _MESH,
                        *(path for item in source['resource_resolution'] for path in item['candidates'])})
        versions = _verify_local(prefix,records,paths)
    return DraftInputs(prefix,run_dir,source,dictionary,output,previous,has_text,versions,remote)
