"""Validate sealed helper-library sources, actual native loading and file deltas."""
import json
from pathlib import PurePosixPath

from . import attempt_workspace as workspace
from .case_file_delta import snapshot, changes, current_source, file_records
from .flotherm_convert_artifacts import relative

CONFIG = '.vibeflow/prepared/flotherm-library.json'
NATIVE = '.vibeflow/flotherm-library/native.json'
RECEIPT = '.vibeflow/flotherm-library/receipt.json'
LOG = '.vibeflow/flotherm-library/native.log'


def artifacts(run, descriptor, output, frozen):
    from .postprocess_artifacts import context
    identity = context(run)
    config = json.loads(workspace.checked_path(run, CONFIG).read_text(encoding='utf8'))
    source = descriptor['source']
    if config != descriptor['configuration'] or {p: r for p, r in frozen.items() if p != CONFIG} != source['files']:
        raise ValueError('FloTHERM library configuration or sealed source changed')
    native = json.loads(workspace.checked_path(run, NATIVE).read_text(encoding='utf8'))
    for key in ('execution_id', 'source_dir', 'destination_dir', 'distro'):
        if native[key] != descriptor[key]:
            raise ValueError('FloTHERM library native invocation differs')
    if (native.get('schema') != 'vibeflow.flotherm-library-native.v1'
            or (descriptor['distro'] != 'native-linux' and native['uid'] == 0)
            or not native['openfoam'] or native['helper_sha256'] != descriptor['native_sha256']
            or native['common_native_sha256'] != descriptor['common_native_sha256']):
        raise ValueError('FloTHERM library native identity/toolchain differs')
    options = native['wm_options']
    if not options or PurePosixPath(options).name != options:
        raise ValueError('FloTHERM library cache identity invalid')
    destination = descriptor['destination_dir']
    logs = {'build': destination+'/flotherm-build.log', 'load': destination+'/flotherm-load.log'}
    if set(native['steps']) != set(logs) or not native['libraries']:
        raise ValueError('FloTHERM library missing native build/load evidence')
    for step, path in logs.items():
        if native['steps'][step]['returncode'] != 0 or workspace.digest_file(workspace.checked_path(run, path)) != native['steps'][step]['log']:
            raise ValueError('FloTHERM library native transcript changed')
    for name, expected in native['libraries'].items():
        path = workspace.checked_path(run, name)
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                raise ValueError('FloTHERM library is not ELF')
        if workspace.digest_file(path) != expected or native['changed'].get(name) != expected:
            raise ValueError('FloTHERM library was not freshly built and loaded')
    observed = snapshot(run)['files']
    if observed != {p: r for p, r in output['files'].items() if p.split('/')[0].casefold() != '.vibeflow'}:
        raise ValueError('FloTHERM library files changed after output capture')
    def mutable(path):
        return (path in native['libraries'] or path in logs.values()
                or path.startswith(destination+'/Make/'+options+'/')
                or path.startswith(destination+'/lnInclude/'))
    for path, expected in source['files'].items():
        if not mutable(path) and observed.get(path) != expected:
            raise ValueError('FloTHERM compilation modified an original input: '+path)
    for path, expected in native['changed'].items():
        if {k: observed[path][k] for k in ('size', 'sha256')} != expected:
            raise ValueError('FloTHERM native output changed')
        if not path.startswith(destination+'/') and path not in native['libraries']:
            raise ValueError('FloTHERM compiler wrote outside its output directories')
    receipt = json.loads(workspace.checked_path(run, RECEIPT).read_text(encoding='utf8'))
    records = {p: observed[p] for p in receipt['outputs']}
    deleted = {p: r for p, r in source['files'].items() if p not in observed}
    if set(deleted) != set(native['deleted']) or any(not mutable(p) for p in deleted):
        raise ValueError('FloTHERM compilation deleted an original input')
    if any(p not in records and source['files'].get(p) != r for p, r in observed.items()):
        raise ValueError('FloTHERM library receipt omitted output')
    expected = {'schema': 'vibeflow.flotherm-library-publication.v1', 'case_id': descriptor['case_id'],
        'job_id': identity['source']['job_id'], 'attempt_id': identity['attempt_id'],
        'execution_id': descriptor['execution_id'], 'inputs': source, 'outputs': records, 'deleted': deleted,
        'native': native, 'worker_sha256': descriptor['worker_sha256'],
        'log': workspace.digest_file(workspace.checked_path(run, LOG))}
    if receipt != expected:
        raise ValueError('FloTHERM library receipt differs from this attempt')
    descriptor['output_files'] = records
    descriptor['deleted_files'] = deleted
    descriptor['receipt'] = receipt
    return records
