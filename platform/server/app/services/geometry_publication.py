"""Registered CadQuery input and artifact contract for durable publication."""
import hashlib
import json

from .. import storage
from . import attempt_workspace as workspace
from .case_evidence import case_input_fingerprint
from .casedict import CASE_DICT_PATH
from .file_read import FileRead, FileReadConflict
from .geo_validation import geometry_declarations, topology_requirement_reason

GENERATED = {'A-geo/scene.json', 'A-geo/validation.json', 'A-geo/geometry_manifest.json',
             'A-geo/toolchain.json'}


def source_snapshot(prefix, generated_paths=()):
    from .case_read_view import inspect
    return inspect(prefix,_source_snapshot,generated_paths,paths=['0-caseDict/caseDict','A-geo'])


def _source_snapshot(root, generated_paths=()):
    text = workspace.checked_path(root,'0-caseDict/caseDict').read_text(encoding='utf8')
    records = workspace.inventory(root, ['A-geo'])
    return describe_source(text, records, generated_paths)


def describe_source(text, records, generated_paths):
    excluded = GENERATED | set(generated_paths)
    selected = {path:record for path, record in records.items() if path.startswith('A-geo/') and path not in excluded and
        not path.startswith(('A-geo/exports/', 'A-geo/logs/')) and '__pycache__' not in path.split('/')}
    return {'case_input_sha256':case_input_fingerprint(text), 'geometry_files':selected}


def artifacts_for(run, descriptor, output, frozen_files):
    if descriptor.get('handler') != 'geometry.v1' or descriptor.get('mode') not in {'preview','export'}:
        raise ValueError('Unregistered publication handler')
    validation = json.loads((run / 'A-geo/validation.json').read_text(encoding='utf-8'))
    if not isinstance(validation, dict):
        raise ValueError('Geometry validation must be an object')
    summary = validation.get('summary', {})
    expected = descriptor['source']
    if (not isinstance(summary, dict) or validation.get('schema') != 'vibeflow.geometry.validation.v1' or
            not summary.get('valid') or summary.get('object_count', 0) < 1 or
            validation.get('source_sha256') != expected['geometry_files']['A-geo/geometry.py']['sha256'] or
            validation.get('case_input_sha256') != expected['case_input_sha256']):
        raise ValueError('Geometry validation does not prove these inputs')
    if 'declared_geometry' in validation:
        dictionary = workspace.checked_path(run, CASE_DICT_PATH).read_text(encoding='utf8')
        if validation['declared_geometry'] != geometry_declarations(dictionary):
            raise ValueError('Geometry validation declarations differ from the case input')
    scene = json.loads((run / 'A-geo/scene.json').read_text(encoding='utf-8'))
    if (not isinstance(scene, dict) or scene.get('schema') != 'vibeflow.cadquery.scene.v1' or
            not isinstance(scene.get('objects'), list) or not scene['objects']):
        raise ValueError('Geometry produced no scene')
    if 'tolerance' in descriptor and (validation.get('tolerance') != descriptor['tolerance'] or
                                     scene.get('tolerance') != descriptor['tolerance']):
        raise ValueError('Geometry artifacts differ from the registered tolerance')
    toolchain = json.loads((run / 'A-geo/toolchain.json').read_text(encoding='utf-8'))
    if not isinstance(toolchain, dict) or not all(toolchain.get(key) for key in ('python','cadquery','ocp','worker_sha256')):
        raise ValueError('Geometry toolchain provenance is incomplete')
    if descriptor.get('execution_id') and (toolchain.get('execution_id') != descriptor['execution_id'] or
            toolchain.get('worker_sha256') != descriptor.get('worker_sha256')):
        raise ValueError('Geometry output was not produced by this prepared worker invocation')
    paths = ['A-geo/validation.json', 'A-geo/scene.json', 'A-geo/toolchain.json']
    if descriptor['mode'] == 'export':
        manifest = json.loads((run / 'A-geo/geometry_manifest.json').read_text(encoding='utf-8'))
        if (not isinstance(manifest, dict) or manifest.get('schema') != 'vibeflow.geometry_manifest.v1' or
                not isinstance(manifest.get('artifacts'), list) or not manifest['artifacts']):
            raise ValueError('Geometry export manifest is empty')
        paths.append('A-geo/geometry_manifest.json')
        for item in manifest['artifacts']:
            for key in ('step','stl'):
                name = workspace.canonical_path(item[key])
                if '/' in name or not name.lower().endswith('.'+key):
                    raise ValueError('Geometry artifact must stay in its exports directory')
                paths.append('A-geo/exports/'+name)
    if output.get('schema') != 'vibeflow.attempt-outputs.v1':
        raise ValueError('Attempt output inventory must precede publication')
    if descriptor['mode'] == 'export':
        # Custom export hooks and inline exports keep their existing relative
        # paths, while edited input scripts/assets never replace user originals.
        paths.extend(path for path, record in output['files'].items() if path.startswith('A-geo/') and
            not path.startswith('A-geo/logs/') and path not in expected['geometry_files'] and
            frozen_files.get(path) != record)
    records = {}
    for path in paths:
        expected_record = output['files'][path]
        actual = workspace.digest_file(workspace.checked_path(run, path))
        if any(actual[key] != expected_record[key] for key in ('size','sha256')):
            raise ValueError('Output changed after attempt inventory')
        records[path] = expected_record
    return records


async def gate_reason(db, case, *, original_dictionary=None):
    from .artifact_publication import current, read_manifest, read_file
    from .workflow_evidence import geometry_gate_reason
    try:
        head, row = await current(db, case.id, 'geometry')
        if head is None:
            return geometry_gate_reason(case.s3_prefix, original_dictionary=original_dictionary)
        if row is None:
            return '当前几何成果尚未发布或已被手工修改，请重新预览验证'
        _, manifest = read_manifest(case.s3_prefix, row)
        if source_snapshot(case.s3_prefix, manifest['descriptor'].get('generated_paths', [])) != manifest['descriptor']['source']:
            return '几何输入已变化，当前成果已过期，请重新预览验证'
        validation = json.loads(read_file(case.s3_prefix, row, 'A-geo/validation.json'))
        summary = validation.get('summary', {})
        if not isinstance(summary, dict) or not summary.get('valid'):
            return '几何验证未通过'
        declarations = validation.get('declared_geometry')
        if not isinstance(declarations, dict):
            from .case_write import bounded_case_read
            declarations = await bounded_case_read(_read_geometry_declarations, case.s3_prefix,
                                                  code='geometry_validation', label='几何要求读取')
        return topology_requirement_reason(declarations, summary)
    except (OSError, ValueError, KeyError, TypeError, FileReadConflict):
        return '几何发布证据缺失或损坏，请重新预览验证'


def _read_geometry_declarations(prefix):
    return geometry_declarations(FileRead(prefix, CASE_DICT_PATH).read().decode('utf8'))
