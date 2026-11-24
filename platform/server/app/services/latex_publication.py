"""Prove native PDF/SyncTeX/BBL, retain sources and read durable LaTeX versions."""
import hashlib
import json
from contextlib import closing, contextmanager
from pathlib import PurePosixPath

from fastapi import HTTPException

from .. import storage
from . import attempt_workspace as workspace,latex_artifacts as artifacts
from .artifact_publication import PublicationIdentity
from .publication_result_reads import Selection
from .postprocess_artifacts import context


def artifacts_for(run,descriptor,output,frozen_files):
    identity=context(run)
    aliases=descriptor.get('input_aliases',{})
    if identity['source']['case_id']!=descriptor['case_id'] or artifacts.source(run,descriptor['main'],descriptor['resources'],
            input_aliases=aliases,validated_gate=True)!=descriptor['source']:
        raise ValueError('LaTeX source differs from its prepared inputs')
    for path,record in frozen_files.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(actual[key]!=record[key] for key in actual):
            raise ValueError('LaTeX modified a sealed input: '+path)
    names=artifacts.paths(descriptor['main'])
    actual_manifest=json.loads(workspace.checked_path(run,names['manifest']).read_text(encoding='utf-8'))
    actual_toolchain=json.loads(workspace.checked_path(run,names['toolchain']).read_text(encoding='utf-8'))
    for key in ('worker_sha256','compiler_sha256'):
        if actual_toolchain.get(key)!=descriptor[key]:
            raise ValueError('LaTeX toolchain differs from its preparation')
    if actual_toolchain['native']['native_sha256']!=descriptor['native_sha256'] or actual_toolchain['native']['runtime']!=descriptor['runtime']:
        raise ValueError('LaTeX native tools differ from their preparation')
    rebuilt,expected_manifest,expected_toolchain=artifacts.generate(run,descriptor,identity,descriptor['runtime'])
    if output.get('schema')!='vibeflow.attempt-outputs.v1':
        raise ValueError('LaTeX output inventory is missing')
    prefix=artifacts.history(descriptor['execution_id'])
    selected={path:value for path,value in output['files'].items() if path in names.values() or path.startswith(prefix)}
    if set(selected)!=set(rebuilt):
        raise ValueError('LaTeX publication is incomplete')
    actual={path:workspace.checked_path(run,path).read_bytes() for path in selected}
    for path,data in actual.items():
        if selected[path]['size']!=len(data) or selected[path]['sha256']!=hashlib.sha256(data).hexdigest():
            raise ValueError('LaTeX artifact changed after capture')
    if artifacts.canonical_pdf(actual[names['pdf']])!=artifacts.canonical_pdf(rebuilt[names['pdf']]):
        raise ValueError('PDF content differs from independent native compilation')
    roots=[artifacts.compile_id_root(run,value['compile_id']) for value in (actual_toolchain,expected_toolchain)]
    normalized=[]
    for files,toolchain,root in zip((actual,rebuilt),(actual_toolchain,expected_toolchain),roots):
        sync,mapping=artifacts.synctex(files[names['synctex']],root,root/PurePosixPath(descriptor['main']).parent/'out',
                                      frozen_files,input_aliases=aliases)
        if mapping!=toolchain['source_mapping']:
            raise ValueError('SyncTeX source mapping differs from its sidecar')
        normalized.append(sync)
    if normalized[0]!=normalized[1]:
        raise ValueError('SyncTeX positions differ from native compilation')
    if artifacts.normalize_log(actual[names['log']],roots[0])!=artifacts.normalize_log(rebuilt[names['log']],roots[1]):
        raise ValueError('LaTeX transcript differs from native compilation')
    for path in set(rebuilt)-{names['pdf'],names['synctex'],names['log'],names['manifest'],names['toolchain']}:
        if actual[path]!=rebuilt[path]:
            raise ValueError('LaTeX bibliography or frozen source copy differs from native compilation')
    expected_manifest['artifacts']={path:{'size':len(actual[path]),'sha256':hashlib.sha256(actual[path]).hexdigest()}
        for path in expected_manifest['artifacts']}
    if actual_manifest!=expected_manifest:
        raise ValueError('LaTeX manifest does not prove its actual input and output')
    for key in ('compile_id','source_mapping'):
        actual_toolchain.pop(key)
        expected_toolchain.pop(key)
    if actual_toolchain!=expected_toolchain:
        raise ValueError('LaTeX recorder or toolchain differs from independent compilation')
    return selected


def write_aliases(prefix,row,manifest):
    from .artifact_publication import read_file
    from .compose_artifacts import existing
    descriptor=manifest['descriptor']
    root=storage._fs_path(prefix,'.')
    for path,expected in descriptor.get('retired_aliases',{}).items():
        data=existing(root,path)
        if data is None: continue
        if len(data)!=expected['size'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
            raise ValueError('Edited prior bibliography was preserved')
        workspace.checked_path(root,path).unlink()
    names=artifacts.paths(descriptor['main'])
    # Source copies belong to history; they never overwrite editable source files.
    ordered=[path for path in names.values() if path in manifest['files'] and path!=names['pdf']]+[names['pdf']]
    for path in ordered:
        existing(root,path)  # Reject links/reparse points before compatibility writes.
        storage._fs_put(prefix,path,read_file(prefix,row,path))


async def _owned_publication(db, case, publication_id, main):
    from sqlalchemy import select
    from ..models import ArtifactPublication, Job, JobAttempt
    from .artifact_publication import current

    if main is not None:
        main = artifacts.main_path(main)
    row = await db.get(ArtifactPublication, publication_id, populate_existing=True) if publication_id else (
        await current(db, case.id, artifacts.channel(main or 'E-report/latex/main.tex')))[1]
    if row is None or row.case_id != case.id or not row.kind.startswith('latex:'):
        raise HTTPException(404, 'LaTeX 发布不存在')
    job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
                          .where(JobAttempt.id == row.attempt_id))
    if (row.status not in {'committed', 'superseded'} or job is None
            or job.case_id != case.id or job.status != 'succeeded'):
        raise ValueError('LaTeX publication has no successful owned Job')
    return row, main, job.id


async def artifact_version(db, case, publication_id, main):
    """Authorize and detach the immutable version before doing archive I/O."""
    row, main, _ = await _owned_publication(db, case, publication_id, main)
    return PublicationIdentity.capture(row), main


@contextmanager
def artifact_reader(prefix, row, main, path, *, source_copy=False):
    """Verify the entire historical bundle without restoring a local cache."""
    from .file_read import ArchivedPublication

    with ArchivedPublication(prefix,row) as archive:
        descriptor, names, document, _ = _archive_documents(archive, row, main)
        if source_copy:
            member = document['source_files'].get(path)
            if member is not None and member != artifacts.history(descriptor['execution_id']) + path:
                raise ValueError('LaTeX history source differs from its archived path')
        else:
            member = path if path in artifacts.paths(document['main']).values() and path in document['artifacts'] else None
        already_checked = {names['manifest'], names['toolchain']}
        if member is not None:
            already_checked.add(member)
        archive.verify_other_than(already_checked)
        if member is None:
            raise HTTPException(404, 'LaTeX source is unavailable' if source_copy else 'LaTeX artifact is unavailable')
        yield archive.member(member)


def _member_bytes(archive, path):
    with closing(archive.member(path).chunks()) as chunks:
        return b''.join(chunks)


def _archive_documents(archive, row, main):
    descriptor = archive.manifest['descriptor']
    if (descriptor.get('handler') != 'latex.v1' or row.kind != artifacts.channel(descriptor['main'])
            or (main and artifacts.channel(main) != row.kind)):
        raise HTTPException(404, 'LaTeX 发布不属于此主文件')
    names = artifacts.paths(descriptor['main'])
    return (descriptor, names, json.loads(_member_bytes(archive, names['manifest'])),
            json.loads(_member_bytes(archive, names['toolchain'])))


async def select_result(db, case, publication_id, main):
    from .artifact_publication import current

    row, main, job_id = await _owned_publication(db, case, publication_id, main)
    _, active = await current(db, case.id, row.kind)
    return await Selection.capture(db, case, row, active, job_id, main=main)


def _retired_alias_absent(prefix, path):
    if not storage._use_s3():
        from .compose_artifacts import existing_path

        return existing_path(storage._fs_path(prefix, '.'), path) is None

    from .file_read import FileRead

    try:
        FileRead(prefix, path)
    except FileNotFoundError:
        return True
    return False


def aliases_applied(prefix, descriptor, records):
    from .case_evidence import matches_current_sha256
    from .compose_artifacts import existing_path
    from .file_read import FileReadConflict

    try:
        names = [path for path in artifacts.paths(descriptor['main']).values() if path in records]
        if not storage._use_s3():
            root = storage._fs_path(prefix, '.')
            if any(existing_path(root, path) is None for path in names):
                return False
        return (all(matches_current_sha256(prefix, path, records[path]['sha256']) for path in names)
                and all(_retired_alias_absent(prefix, path) for path in descriptor.get('retired_aliases', {})))
    except TimeoutError:
        raise
    except (OSError, ValueError, FileReadConflict):
        return False


def read_result_archive(selection):
    from .file_read import ArchivedPublication

    row = selection.publication
    with ArchivedPublication(selection.prefix, row) as archive:
        descriptor, names, document, toolchain = _archive_documents(archive, row, selection.main)
        archive.verify_other_than({names['manifest'], names['toolchain']})
        records = archive.manifest['files']
    applied = selection.current and aliases_applied(selection.prefix, descriptor, records)
    return {'job_id': selection.job_id, 'pdf': names['pdf'], 'manifest': document, 'toolchain': toolchain,
            'publication': {**selection.publication_fields(), 'aliases_applied': applied}}


async def synctex_registered_main(db, case_id, pdf):
    from ..models import ArtifactPublicationHead

    path = PurePosixPath(pdf)
    if path.parent.name != 'build' or path.suffix != '.pdf':
        return None
    main = (path.parent.parent / (path.stem + '.tex')).as_posix()
    try:
        kind = artifacts.channel(main)
    except ValueError:
        return None
    head = await db.get(ArtifactPublicationHead, (case_id, kind), populate_existing=True)
    return main if head is not None else None


async def synctex_version(db,case,pdf,publication_id=None):
    main = None
    if publication_id is None:
        main = await synctex_registered_main(db, case.id, pdf)
        if main is None:
            return None
    try:
        return await artifact_version(db,case,publication_id,main)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code': 'evidence_invalid', 'message': 'LaTeX 发布记录或文件损坏'}) from error


def synctex_snapshot(prefix, version, pdf):
    from .file_read import ArchivedPublication, FileReadConflict
    from .synctex_native import MAX_PDF,MAX_SIDECAR
    row, main = version
    try:
        with ArchivedPublication(prefix, row) as archive:
            descriptor, names, manifest, toolchain = _archive_documents(archive, row, main)
            if pdf != names['pdf']:
                raise HTTPException(404, 'PDF 不属于所选 LaTeX 发布')
            sidecar = names['synctex']
            for path, limit in ((pdf, MAX_PDF), (sidecar, MAX_SIDECAR)):
                if archive.member(path).size > limit:
                    raise HTTPException(422, 'SyncTeX snapshot exceeds its limit')
            archive.verify_other_than({pdf, sidecar, names['manifest'], names['toolchain']})
            return {'publication_id': row.id, 'manifest_sha256': row.manifest_sha256,
                    'document': {'manifest': manifest, 'toolchain': toolchain},
                    'distro': descriptor['distro'], 'pdf_bytes': _member_bytes(archive, pdf),
                    'sidecar_bytes': _member_bytes(archive, sidecar)}
    except (FileReadConflict, OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(409, {'code': 'evidence_invalid', 'message': 'LaTeX 发布记录或文件损坏'}) from error


def diagnostic_path(value,main,input_aliases=None):
    """Map native temporary names back to the owned source tree, including errors."""
    import posixpath,re
    if value is None: return None
    value=value.replace('\\','/')
    if input_aliases and value in input_aliases:
        return input_aliases[value]
    marker=re.search(r'\.latex-compile-[0-9a-f]{32}/case/(.*)',value)
    if marker: relative=marker[1]
    elif value.startswith('/') or re.match(r'^[A-Za-z]:',value): return None
    else: relative=posixpath.join(str(PurePosixPath(main).parent),value)
    relative=posixpath.normpath(relative)
    if relative=='..' or relative.startswith('../'): return None
    return relative
