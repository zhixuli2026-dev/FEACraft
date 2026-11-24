"""Select read-only sources, run an owned native query, then recheck its meaning."""
import asyncio
import io
from dataclasses import dataclass
from math import isfinite
import posixpath

from fastapi import HTTPException
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .. import storage
from . import synctex_execution,synctex_service
from .artifact_publication import PublicationIdentity
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, bounded_case_read


async def select(db,case,pdf,publication_id=None):
    from ..models import CaseEventCounter
    from .latex_publication import synctex_version

    version = await synctex_version(db, case, pdf, publication_id)
    counter = await db.get(CaseEventCounter, case.id, populate_existing=True) if version is None else None
    return (case.id, case.s3_prefix, version, counter.next_seq if counter else None)


def prepare(selection, pdf, expected_file_version):
    from .file_read import FileRead
    from .latex_publication import synctex_snapshot
    from .openfoam_runtime import distribution
    case_id, prefix, version, _ = selection
    if expected_file_version is not None:
        try:
            matches = FileRead(prefix, pdf).version == expected_file_version
        except FileNotFoundError:
            matches = False
        if not matches:
            raise HTTPException(409, {'code': 'file_changed_during_read', 'message': 'PDF 已变化，请重新读取后定位源码'})
    if version is None:
        selected=synctex_service.legacy_inputs(prefix,pdf)
        selected.update(publication_id=None,distro=distribution())
    else:
        selected=synctex_snapshot(prefix,version,pdf)
    return {**selected,'pdf':pdf,'case_id':case_id,'prefix':prefix}


def pdf_point(pdf, page, x, y):
    try:
        media = PdfReader(io.BytesIO(pdf)).pages[page - 1].mediabox
        left, bottom, right, top = (float(value) for value in media)
    except (IndexError, KeyError, TypeError, ValueError, PdfReadError) as error:
        raise ValueError('无法核验 SyncTeX 所选 PDF 的页面尺寸') from error
    if (not all(isfinite(value) for value in (left, bottom, right, top))
            or not (left < x < right and bottom < y < top)):
        raise ValueError('SyncTeX 定位坐标超出 PDF 页面')
    # PDF.js has already inverted the display rotation and zoom. SyncTeX uses
    # the top-left of the original page, even when the viewer clips it.
    return x - left, top - y


async def execute(selected,user_id,page,x,y,*,coordinate_space='page'):
    from .native_request_execution import scope
    from .fs_directory import remove_owned
    from .node_storage_budget import allocation
    if coordinate_space == 'pdf':
        x, y = await finish_before_cancel(asyncio.to_thread(pdf_point, selected['pdf_bytes'], page, x, y))
    pdf,sidecar=selected['pdf_bytes'],selected['sidecar_bytes']
    async with allocation(len(pdf)+len(sidecar)+synctex_execution.MAX_SETTINGS_BYTES):
        root=synctex_execution.directory()
        try:
            profile=await finish_before_cancel(asyncio.to_thread(synctex_execution.prepare,pdf,
                sidecar,selected['distro'],page,x,y,root=root))
            async with scope(selected['case_id'],user_id,root,'synctex.query',profile) as executor:
                return await executor.run_sync(synctex_execution.run,profile,timeout=30)
        finally:
            await finish_before_cancel(asyncio.to_thread(remove_owned,root,root=root.parent))


@dataclass(frozen=True)
class ResultSelection:
    case_id: int
    prefix: str
    revision: int
    sequence: int | None
    publication: PublicationIdentity | None = None
    status: str | None = None
    current: bool = False


async def select_result(db, case, selected):
    """Detach the DB binding before checking archive bytes or live sources."""
    from ..models import ArtifactPublication, CaseEventCounter
    from .artifact_publication import current
    from .latex_publication import synctex_registered_main, synctex_version

    if case.s3_prefix != selected['prefix']:
        raise CaseWriteConflict('input_stale', 'SyncTeX 算例存储位置已变化')
    counter = await db.get(CaseEventCounter, case.id, populate_existing=True)
    base = (case.id, case.s3_prefix, case.dict_revision, counter.next_seq if counter else None)
    if selected['publication_id'] is None:
        if await synctex_registered_main(db, case.id, selected['pdf']) is not None:
            raise CaseWriteConflict('input_stale', 'PDF registration changed during SyncTeX query')
        return ResultSelection(*base)
    publication, _ = await synctex_version(db, case, selected['pdf'], selected['publication_id'])
    if publication.manifest_sha256 != selected['manifest_sha256']:
        raise HTTPException(409, {'code':'evidence_invalid','message':'SyncTeX publication changed during query'})
    row = await db.get(ArtifactPublication, publication.id, populate_existing=True)
    try:
        _, active = await current(db, case.id, publication.kind)
    except ValueError as error:
        raise HTTPException(409, {'code':'evidence_invalid','message':'LaTeX 发布记录或文件损坏'}) from error
    return ResultSelection(*base, publication, row.status, active is not None and active.id == row.id)


def _legacy_location(prefix, selected, parsed):
    from .attempt_workspace import checked_path, digest_file
    from .request_deadline import current

    root=storage._fs_path(prefix,'.')
    deadline=current()
    for path,digest in selected['files'].items():
        try:matches=digest_file(checked_path(root,path), deadline=deadline)['sha256']==digest
        except FileNotFoundError:matches=False
        if not matches:raise CaseWriteConflict('input_stale','PDF or SyncTeX sidecar changed during query')
    source=synctex_service.map_synctex_input(parsed['input'],selected['pdf'])
    expected=selected['sources'].get(source)
    if expected is None:raise ValueError('SyncTeX source was not present in the selected case')
    try:observed=digest_file(checked_path(root,source), deadline=deadline)
    except FileNotFoundError:observed=None
    if observed!=expected:raise CaseWriteConflict('input_stale','SyncTeX source changed during query')
    return source


def _live_matches(prefix, path, digest):
    from .case_evidence import matches_current_sha256
    from .file_read import FileReadConflict

    try:
        return matches_current_sha256(prefix, path, digest)
    except (OSError, ValueError, FileReadConflict):
        return False


def _publication_location(selection, selected, parsed):
    from .file_read import ArchivedPublication, FileReadConflict
    from .latex_publication import aliases_applied

    row = selection.publication
    document = selected['document']
    mappings = {posixpath.normpath(key):value for key,value in document['toolchain']['source_mapping'].items()}
    source = mappings.get(posixpath.normpath(parsed['input'].replace('\\','/')))
    path = document['manifest']['source_files'].get(source)
    if path is None:
        raise ValueError('SyncTeX location does not refer to a frozen case source')
    try:
        with ArchivedPublication(selection.prefix, row) as archive:
            archive.verify_other_than(set())
            files = archive.manifest['files']
            digest = files[path]['sha256']
            descriptor = archive.manifest['descriptor']
    except TimeoutError:
        raise
    except (OSError, ValueError, KeyError, TypeError, FileReadConflict) as error:
        raise HTTPException(409, {'code':'evidence_invalid','message':'LaTeX 发布记录或文件损坏'}) from error

    return {
        'file': source, 'publication_id': row.id, 'source_sha256': digest,
        'source_current_matches': _live_matches(selection.prefix, source, digest),
        'publication': {'id': row.id, 'generation': row.generation, 'status': selection.status,
                        'current': selection.current, 'aliases_applied': selection.current
                        and aliases_applied(selection.prefix, descriptor, files)},
    }


async def result(db, selection, selected, output, page):
    from ..models import Case
    from .publication_inputs import resolve

    parsed = synctex_service.parse_synctex_edit(output)
    options = {'code':'synctex_result', 'label':'SyncTeX 来源核验'}
    location = {'line':parsed['line'], 'column':parsed['column'], 'page':page}
    if selection.publication is None:
        source = await bounded_case_read(_legacy_location, selection.prefix, selected, parsed, **options)
        return {**location, 'file':source}

    result = await bounded_case_read(_publication_location, selection, selected, parsed, **options)
    stale = True
    if selection.current:
        case = await db.get(Case, selection.case_id, populate_existing=True)
        if case is None or case.s3_prefix != selection.prefix:
            raise CaseWriteConflict('input_stale', 'SyncTeX 算例存储位置已变化')
        try:
            await resolve(db, case, [{'publication_id':selection.publication.id, 'paths':None,
                                      'require_current':True}], materialize=False)
            stale = False
        except TimeoutError:
            raise
        except (OSError, ValueError, KeyError):
            pass
    result['publication']['stale'] = stale
    return {**location, **result}
