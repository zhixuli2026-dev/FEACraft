"""Reproducible report drafts and guarded writes to the editable report file."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from .. import storage
from . import attempt_workspace as workspace, report_artifacts, report_builder
from .casedict import parse_casedict
from .postprocess_artifacts import context

OUTPUTS={'E-report/report.md':('report.compose','report-source'),
    'E-report/report.generated.md':('report.compose-generated','report-source-generated')}


def directory(execution_id):
    if str(uuid.UUID(execution_id))!=execution_id:
        raise ValueError('Invalid composition invocation')
    return 'E-report/compositions/'+execution_id


def record(data):
    return {'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}


def existing_path(root,path):
    """Check every existing parent even when the final file is absent."""
    current=root
    for part in workspace.canonical_path(path).split('/'):
        current=current/part
        try:
            workspace.ordinary(current)
        except FileNotFoundError:
            return None
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('Composition destination is not an ordinary owned file')
    return current


def existing(root,path):
    target=existing_path(root,path)
    return target.read_bytes() if target is not None else None


def live_existing(prefix,path):
    path=workspace.canonical_path(path)
    if storage._use_s3():
        from .s3_case_files import read_version
        return read_version(prefix,path)[0]
    return existing(storage._fs_path(prefix,'.'),path)


def source(root,*,validated_gate=False,standalone=False,gate_prefix=None,track_resolution=False,load=None):
    return report_artifacts.source_snapshot(root,'compose',None,validated_gate=validated_gate,
        standalone=standalone,gate_prefix=gate_prefix,track_resolution=track_resolution,load=load)


def produce(root,execution_id,output,case_name,generated_at=None):
    if output not in OUTPUTS:
        raise ValueError('Unsupported composition destination')
    identity=context(root)
    parsed=parse_casedict((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))
    case=SimpleNamespace(s3_prefix=root.relative_to(storage._fs_root().resolve()).as_posix(),
        id=identity['source']['case_id'],name=case_name,workflow_state=parsed.get('workflowState','E'))
    document=report_builder.compose_report(case,validated_gate=True,generated_at=generated_at,root=root)
    if not document['evidence']['report_ready']:
        raise ValueError('Frozen composition evidence is not report-ready')
    folder=directory(execution_id)
    previous=existing(root,output)
    backup=folder+'/previous.md' if previous is not None else None
    result={**document,'path':output,'backup':backup}
    files={folder+'/report.md':document['content'].encode('utf-8'),
        folder+'/result.json':report_artifacts.encoded(result)}
    if backup:
        files[backup]=previous
    toolchain={'schema':'vibeflow.report-compose.v1','execution_id':execution_id,
        'case_id':identity['source']['case_id'],'job_id':identity['source']['job_id'],
        'attempt_id':identity['attempt_id'],'case_name':case_name,'output':output,
        'input_publications':identity.get('publications',[]),'inputs':identity['files'],
        'target_before':record(previous) if previous is not None else None,
        'artifacts':{path:record(data) for path,data in files.items()},
        'worker_sha256':hashlib.sha256(Path(__file__).with_name('registered_compose_worker.py').read_bytes()).hexdigest(),
        'composer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'builder_sha256':hashlib.sha256(Path(report_builder.__file__).read_bytes()).hexdigest()}
    if generated_at is not None:
        toolchain['generated_at']=generated_at
    files[folder+'/toolchain.json']=report_artifacts.encoded(toolchain)
    return files,toolchain


def artifacts_for(run,descriptor,output,frozen_files):
    identity=context(run)
    if identity['source']['case_id']!=descriptor['case_id'] or source(run,validated_gate=True,
            standalone=not descriptor.get('registered_postprocess',True),
            track_resolution='resource_resolution' in descriptor['source'])!=descriptor['source']:
        raise ValueError('Composition inputs differ from their preparation')
    for path,expected in frozen_files.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(actual[key]!=expected[key] for key in ('size','sha256')):
            raise ValueError('Composition modified its frozen input: '+path)
    rebuilt,toolchain=produce(run,descriptor['execution_id'],descriptor['output'],descriptor['case_name'],descriptor.get('generated_at'))
    for key in ('target_before','worker_sha256','composer_sha256','builder_sha256'):
        if toolchain[key]!=descriptor[key]:
            raise ValueError('Composition provenance differs from its preparation: '+key)
    if output.get('schema')!='vibeflow.attempt-outputs.v1':
        raise ValueError('Composition output inventory is missing')
    prefix=directory(descriptor['execution_id'])+'/'
    selected={path:value for path,value in output['files'].items() if path.startswith(prefix)}
    if set(selected)!=set(rebuilt):
        raise ValueError('Composition publication is incomplete')
    for path,data in rebuilt.items():
        if workspace.checked_path(run,path).read_bytes()!=data or any(selected[path][key]!=value for key,value in record(data).items()):
            raise ValueError('Composition content or backup differs from independent reconstruction')
    return selected


def before_publish(prefix,descriptor):
    data=live_existing(prefix,descriptor['output'])
    if (record(data) if data is not None else None)!=descriptor['target_before']:
        raise ValueError('Report draft changed while composition was running')


def write_aliases(prefix,row,manifest):
    from .artifact_publication import read_file
    root=storage._fs_path(prefix,'.')
    descriptor=manifest['descriptor']
    files={path:read_file(prefix,row,path) for path in manifest['files']}
    generated=files[directory(descriptor['execution_id'])+'/report.md']
    target=existing(root,descriptor['output'])
    # Repeated repair is allowed only for the original or already-applied bytes.
    if target!=generated and (record(target) if target is not None else None)!=descriptor['target_before']:
        raise ValueError('Edited report preserved; composition was not applied')
    for path,data in files.items():
        present=existing(root,path)
        if present is not None and present!=data:
            raise ValueError('Edited composition history preserved: '+path)
    for path,data in files.items():
        storage._fs_put(prefix,path,data)
    # Keep the exact old bytes in the immutable bundle and mirror before replacing.
    storage._fs_put(prefix,descriptor['output'],generated)


def applied(prefix,row,manifest):
    from .artifact_publication import read_file
    descriptor=manifest['descriptor']
    if live_existing(prefix,descriptor['output'])!=read_file(prefix,row,directory(descriptor['execution_id'])+'/report.md'):
        return False
    return all(live_existing(prefix,path)==read_file(prefix,row,path) for path in manifest['files'])
