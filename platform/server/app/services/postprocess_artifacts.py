"""Shared deterministic configuration and ownership for registered extraction."""
import hashlib
import json
from pathlib import PurePosixPath
import uuid

from .case_evidence import case_input_fingerprint, file_record, safe_run_dir
from .casedict import parse_casedict
from .postprocess_executor import config_from_case_dict, postprocess_fingerprint, reference_input_paths
from .postprocess_reference_inputs import bind_previous_references

LATEST = 'D-post/data/latest-postprocess.json'
TOOLCHAIN = 'D-post/data/postprocess-toolchain.json'


def config_path(job_id):
    return f'D-post/configs/postprocess-{job_id}.json'


def result_dir(job_id):
    return f'D-post/data/postprocess-{job_id}'


def configuration(root,run_dir,*,case_inputs=False):
    run_dir=safe_run_dir(run_dir)
    text=(root/'0-caseDict/caseDict').read_text(encoding='utf-8')
    parsed=parse_casedict(text)
    requested=(parsed.get('postProcessing') or {}).get('runDir')
    if not case_inputs and requested and safe_run_dir(requested)!=run_dir:
        raise ValueError('Postprocessing runDir differs from its workflow')
    return config_from_case_dict(parsed,run_dir)


def reference_paths(config):
    paths=reference_input_paths(config)
    if any(PurePosixPath(path).parts[0].casefold()=='.vibeflow' for path in paths):
        raise ValueError('Reference fields cannot select execution metadata')
    return paths


def snapshot(root,run_dir,*,case_inputs=False):
    config=configuration(root,run_dir,case_inputs=case_inputs)
    value={'run_dir':safe_run_dir(run_dir),
        'case_input_sha256':case_input_fingerprint((root/'0-caseDict/caseDict').read_text(encoding='utf-8')),
        'postprocess_sha256':postprocess_fingerprint(config),
        'reference_files':[file_record(root,root/path) for path in reference_paths(config)]}
    if case_inputs:
        from .case_evidence import run_inventory
        value['run_inputs']=[file_record(root,path) for path in run_inventory(root,run_dir)]
    return value


def context(root):
    manifest=json.loads((root.parent/'manifest.json').read_text(encoding='utf-8'))
    source=manifest['source']
    if (root.name!='run' or manifest.get('schema')!='vibeflow.attempt-inputs.v1' or
            str(uuid.UUID(manifest['attempt_id']))!=root.parent.name or
            any(type(source.get(key)) is not int or source[key]<=0 for key in ('case_id','job_id'))):
        raise ValueError('Postprocessing requires an owned sealed attempt')
    return manifest


def derived_config(root,run_dir,execution_id,*,case_inputs=False):
    manifest=context(root)
    config=configuration(root,run_dir,case_inputs=case_inputs)
    raw=(root/'0-caseDict/caseDict').read_bytes()
    config['_source']={'path':'0-caseDict/caseDict','sha256':hashlib.sha256(raw).hexdigest(),
        'case_input_sha256':case_input_fingerprint(raw.decode('utf-8')),
        'dict_revision':manifest['source']['dict_revision'],'postprocess_sha256':postprocess_fingerprint(config),
        'execution_id':execution_id,
        'input_publications':manifest.get('publications',[])}
    if not case_inputs:
        from .attempt_workspace import checked_path
        from .openfoam_dynamic_refinement import requested

        settings=requested(raw.decode('utf-8'))
        if settings is not None:
            native=json.loads(checked_path(root,run_dir+'/solve-evidence.json').read_text(encoding='utf-8'))
            if native.get('configuration',{}).get('dynamic_refinement')!=settings or 'dynamic_refinement' not in native:
                raise ValueError('Native refinement evidence differs from the sealed solve configuration')
            config['_source']['dynamic_refinement']={'field':settings['field'],**native['dynamic_refinement']}
    bind_previous_references(root, manifest, config)
    return config


def managed(path,job_id):
    return path in {LATEST,TOOLCHAIN,config_path(job_id)} or path.startswith(result_dir(job_id)+'/')
