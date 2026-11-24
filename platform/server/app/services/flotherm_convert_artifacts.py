"""Validate complete FloTHERM conversions before publishing their file set."""
import hashlib
import json
from pathlib import Path
import tempfile

from . import attempt_workspace as workspace
from .case_file_delta import snapshot, current_source
from .flotherm_convert_job import convert
from .flotherm_flow_devices import FIXED_FILES
from .flotherm_two_resistance import NATIVE_FILES

CONFIG = '.vibeflow/prepared/flotherm-convert.json'
RECEIPT = '.vibeflow/flotherm-convert/receipt.json'
ENGINE_FILES = ('flotherm_convert_job.py', 'flotherm_case.py', 'flotherm_channel_case.py', 'flotherm_ir.py',
                'flotherm_geometry.py', 'flotherm_snappy.py', 'flotherm_flow_devices.py', 'flotherm_parts.py',
                'flotherm_refinement.py', 'flotherm_merge.py', 'flotherm_restart_runtime.py', 'flotherm_clean_runtime.py',
                'flotherm_conduction_gate.py',
                'flotherm_settings.py', 'flotherm_coverage.py', 'flotherm_output.py','flotherm_two_resistance.py',
                *(f'flotherm_two_r_native/{name}' for name in NATIVE_FILES),
                *(f'flotherm_fixed_flow_native/{name}' for name in FIXED_FILES))


def engine_hashes():
    return {name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
            for name in ENGINE_FILES}


def relative(value):
    value = workspace.canonical_path(value)
    if value.split('/')[0].casefold() == '.vibeflow':
        raise ValueError('FloTHERM paths cannot select execution metadata')
    return value


def empty_output(root, output):
    path = root / relative(output)
    if path.exists():
        workspace.checked_path(root, output)
        if not path.is_dir() or any(path.iterdir()):
            raise FileExistsError('输出目录非空，请换空目录；已有内容不会被覆盖')
    # Check existing parents as well, including when the output does not exist.
    for parent in path.parents:
        if parent == root:
            break
        if parent.exists():
            workspace.checked_path(root, parent.relative_to(root).as_posix())


def artifacts(run, descriptor, output, frozen):
    from .postprocess_artifacts import context
    identity = context(run)
    configuration = json.loads(workspace.checked_path(run, CONFIG).read_text(encoding='utf8'))
    if configuration != descriptor['configuration'] or engine_hashes() != configuration['engine_sha256']:
        raise ValueError('FloTHERM configuration or generator changed')
    source = descriptor['source']
    if {p: r for p, r in frozen.items() if p != CONFIG} != source['files']:
        raise ValueError('FloTHERM sealed inputs differ')
    observed = snapshot(run)['files']
    captured = {p: r for p, r in output['files'].items() if p.split('/')[0].casefold() != '.vibeflow'}
    if observed != captured:
        raise ValueError('FloTHERM files changed after output capture')
    if any(observed.get(p) != r for p, r in source['files'].items()):
        raise ValueError('FloTHERM conversion changed an original input')
    records = {p: r for p, r in observed.items() if p not in source['files']}
    target = descriptor['output_dir']
    if not records or any(not p.startswith(target + '/') for p in records):
        raise ValueError('FloTHERM wrote outside its selected output directory')
    # Replay the deterministic generator in a validator-owned temporary tree.
    # This checks every generated dictionary, report, run script and marker,
    # including settings-derived IR and explicit unsupported-physics blockers.
    with tempfile.TemporaryDirectory(prefix='flotherm-validate-') as temp:
        check = Path(temp)
        for name in (configuration['ir_path'], configuration['settings_path']):
            if name:
                dest = check / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(workspace.checked_path(run, name).read_bytes())
        convert(check, configuration['ir_path'], configuration['settings_path'], target)
        expected = workspace.inventory(check, [target])
    if records != expected:
        raise ValueError('FloTHERM generated artifacts differ from the sealed conversion')
    receipt = json.loads(workspace.checked_path(run, RECEIPT).read_text(encoding='utf8'))
    required = {'schema': 'vibeflow.flotherm-convert-publication.v1',
        'case_id': descriptor['case_id'], 'job_id': identity['source']['job_id'],
        'attempt_id': identity['attempt_id'], 'execution_id': descriptor['execution_id'],
        'inputs': source, 'outputs': records, 'engine_sha256': configuration['engine_sha256'],
        'worker_sha256': descriptor['worker_sha256']}
    if receipt != required:
        raise ValueError('FloTHERM result receipt differs from this attempt')
    descriptor['output_files'] = records
    descriptor['deleted_files'] = {}
    descriptor['receipt'] = receipt
    return records
