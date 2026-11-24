"""Validate native document contents and read owned immutable report versions."""
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from . import attempt_workspace as workspace, report_artifacts as artifacts
from .artifact_publication import PublicationIdentity
from .postprocess_artifacts import context
from .publication_result_reads import Selection, archive_json


def artifacts_for(run,descriptor,output,frozen_files):
    identity=context(run)
    case_id,job_id=(identity['source'][key] for key in ('case_id','job_id'))
    if case_id!=descriptor['case_id'] or artifacts.source_snapshot(run,descriptor['mode'],descriptor['pandoc'],
            validated_gate=True,standalone=not descriptor.get('registered_postprocess',True),
            track_resolution='resource_resolution' in descriptor['source'])!=descriptor['source']:
        raise ValueError('Report source differs from its prepared inputs')
    for path,record in frozen_files.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(actual[key]!=record[key] for key in ('size','sha256')):
            raise ValueError('Report modified a sealed input: '+path)
    if workspace.digest_file(Path(descriptor['pandoc']))['sha256']!=descriptor['pandoc_sha256']:
        raise ValueError('Prepared Pandoc executable changed')
    markdown,document,_,engineering=artifacts.document(run,descriptor['mode'],descriptor['pandoc'],descriptor.get('generated_at'))
    with artifacts.compilation(run) as folder:
        rebuilt=artifacts.compile_document(run,document,descriptor['pandoc'],folder)
    for path,data in rebuilt.items():
        actual=(run/path).read_bytes()
        if path.endswith('.docx'):
            if artifacts.canonical_docx(actual)!=artifacts.canonical_docx(data):
                raise ValueError('DOCX body/resources differ from native recompilation')
        elif actual!=data:
            raise ValueError('HTML body/resources differ from native recompilation')
    source=artifacts.source_path(job_id)
    if (run/source).read_bytes()!=markdown:
        raise ValueError('Published Markdown differs from its frozen source')
    common={'case_id':case_id,'job_id':job_id,'attempt_id':identity['attempt_id'],
        'execution_id':descriptor['execution_id'],'mode':descriptor['mode'],'source_path':source,
        'input_publications':identity.get('publications',[])}
    if descriptor.get('generated_at') is not None:
        common['generated_at']=descriptor['generated_at']
    manifest=json.loads((run/artifacts.MANIFEST).read_text(encoding='utf-8'))
    records={path:workspace.digest_file(workspace.checked_path(run,path)) for path in (*artifacts.OUTPUTS,source)}
    expected={**common,'schema':'vibeflow.report-publication.v1','inputs':frozen_files,
        'document_sha256':hashlib.sha256(artifacts.encoded(document)).hexdigest(),'artifacts':records}
    if engineering is not None:
        expected['engineering_report']=engineering
    if manifest!=expected:
        raise ValueError('Report manifest does not prove its actual source and outputs')
    toolchain=json.loads((run/artifacts.TOOLCHAIN).read_text(encoding='utf-8'))
    expected={**common,'schema':'vibeflow.report-toolchain.v1',**{key:descriptor[key] for key in
        ('pandoc','pandoc_sha256','worker_sha256','compiler_sha256','builder_sha256')}}
    if any(toolchain.get(key)!=value for key,value in expected.items()) or not all(toolchain.get(key) for key in ('python','pandoc_version')):
        raise ValueError('Report toolchain does not belong to this invocation')
    if output.get('schema')!='vibeflow.attempt-outputs.v1':
        raise ValueError('Report output inventory is missing')
    required={*artifacts.OUTPUTS,source,artifacts.MANIFEST,artifacts.TOOLCHAIN}
    selected={path:record for path,record in output['files'].items() if artifacts.managed(path,job_id)}
    if set(selected)!=required:
        raise ValueError('Report publication is incomplete')
    for path,record in selected.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(actual[key]!=record[key] for key in ('size','sha256')):
            raise ValueError('Report changed after output capture')
    return selected


async def _owned_publication(db, case, publication_id):
    from fastapi import HTTPException
    from sqlalchemy import select
    from ..models import ArtifactPublication, Job, JobAttempt
    from .artifact_publication import current

    _, active = await current(db, case.id, 'report')
    row = await db.get(ArtifactPublication, publication_id, populate_existing=True) if publication_id else active
    if row is None or row.case_id != case.id or row.kind != 'report':
        raise HTTPException(404, '报告发布不存在')
    job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
                          .where(JobAttempt.id == row.attempt_id))
    if (row.status not in {'committed', 'superseded'} or job is None
            or job.case_id != case.id or job.status != 'succeeded'):
        raise ValueError('Report publication has no successful owned Job')
    return row, active, job.id


async def artifact_version(db, case, publication_id):
    """Bind the report archive and its successful producer under case control."""
    row, _, _ = await _owned_publication(db, case, publication_id)
    return PublicationIdentity.capture(row)


async def select_result(db, case, publication_id):
    row, active, job_id = await _owned_publication(db, case, publication_id)
    return await Selection.capture(db, case, row, active, job_id)


@contextmanager
def artifact_reader(prefix, row, path):
    """Verify the archive while opening only the selected output as a stream."""
    from fastapi import HTTPException
    from .file_read import ArchivedPublication
    from .publication_inputs import _selected_members

    with ArchivedPublication(prefix, row) as archive:
        _selected_members(archive.manifest, None)

        document = archive_json(archive, artifacts.MANIFEST)
        archive_json(archive, artifacts.TOOLCHAIN)
        allowed = path in document['artifacts']
        already_read = {artifacts.MANIFEST, artifacts.TOOLCHAIN}
        if allowed:
            already_read.add(path)
        archive.verify_other_than(already_read)
        if not allowed:
            raise HTTPException(404, '报告产物不存在')
        yield archive.member(path)


def read_result_archive(selection):
    from .file_read import ArchivedPublication
    from .publication_inputs import _selected_members

    with ArchivedPublication(selection.prefix, selection.publication) as archive:
        _selected_members(archive.manifest, None)
        document = archive_json(archive, artifacts.MANIFEST)
        toolchain = archive_json(archive, artifacts.TOOLCHAIN)
        archive.verify_other_than({artifacts.MANIFEST, artifacts.TOOLCHAIN})
    return {'manifest':document, 'toolchain':toolchain, 'publication':selection.publication_fields()}
