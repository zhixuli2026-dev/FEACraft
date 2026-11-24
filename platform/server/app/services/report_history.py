"""Owned draft/PPT/numbered-PDF history, distinct from mutable file aliases."""
from contextlib import closing, contextmanager
import json

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from ..models import ArtifactPublication, ArtifactPublicationHead, JobAttempt
from . import compose_artifacts


def family(kind):
    if kind in {value[1] for value in compose_artifacts.OUTPUTS.values()}:return 'draft'
    if kind.startswith('pdf-number:'):return 'pdf-number'
    if kind.startswith('ppt:'):return 'ppt'
    return None


def condition(selected=None):
    choices={'draft':ArtifactPublication.kind.in_([v[1] for v in compose_artifacts.OUTPUTS.values()]),
             'pdf-number':ArtifactPublication.kind.startswith('pdf-number:'),
             'ppt':ArtifactPublication.kind.startswith('ppt:')}
    return choices[selected] if selected else or_(*choices.values())


async def page(db,case,*,selected=None,output=None,before_id=None,limit=50):
    """Metadata only. Every selected version is independently verified by result."""
    scope=[ArtifactPublication.case_id==case.id,condition(selected)]
    if output is not None:
        if output not in compose_artifacts.OUTPUTS or selected!='draft':
            raise HTTPException(400,'报告草稿输出路径不受支持')
        scope.append(ArtifactPublication.kind==compose_artifacts.OUTPUTS[output][1])
    query=select(ArtifactPublication,JobAttempt.publication_json,ArtifactPublicationHead.active_publication_id).join(
        JobAttempt,JobAttempt.id==ArtifactPublication.attempt_id).outerjoin(ArtifactPublicationHead,and_(
            ArtifactPublicationHead.case_id==ArtifactPublication.case_id,ArtifactPublicationHead.kind==ArtifactPublication.kind)).where(*scope)
    if before_id:
        cursor=await db.scalar(select(ArtifactPublication).where(*scope,ArtifactPublication.id==before_id))
        if cursor is None:raise HTTPException(400,'历史游标不属于所选报告范围')
        query=query.where(or_(ArtifactPublication.created_at<cursor.created_at,and_(
            ArtifactPublication.created_at==cursor.created_at,ArtifactPublication.id<cursor.id)))
    rows=await db.execute(query.order_by(ArtifactPublication.created_at.desc(),ArtifactPublication.id.desc()).limit(limit))
    values=[]
    for row,reservation,active in rows:
        try:
            label=json.loads(reservation)['descriptor']['output']
            if not isinstance(label,str):label=None
        except (ValueError,KeyError,TypeError):label=None
        values.append({'id':row.id,'kind':row.kind,'family':family(row.kind),'generation':row.generation,
            'status':row.status,'attempt_id':row.attempt_id,'current':active==row.id,
            'output':label,'created_at':row.created_at.isoformat()})
    return values


async def result(db,case,publication_id):
    from .artifact_publication import current,read_manifest
    from .publication_inputs import resolve
    from .step_handlers import publication_handler,kind_for
    row=await db.get(ArtifactPublication,publication_id)
    if row is None or row.case_id!=case.id or family(row.kind) is None:
        raise HTTPException(404,'报告产物版本不存在')
    try:
        if family(row.kind)=='draft':
            from .compose_publication import result as draft_result
            row,document=await draft_result(db,case,row.id)
            _,manifest=read_manifest(case.s3_prefix,row)
            paths=[document['source_path']]+([document['backup']] if document.get('backup') else [])
            state=document['publication'];output=document['path']
        else:
            await resolve(db,case,[{'publication_id':row.id,'require_current':False}])
            _,manifest=read_manifest(case.s3_prefix,row)
            descriptor=manifest['descriptor']
            handler=publication_handler(descriptor)
            if kind_for(handler,descriptor)!=row.kind:raise ValueError('Publication channel mismatch')
            output=descriptor['output'];paths=[output]
            _,active=await current(db,case.id,row.kind)
            try:
                await resolve(db,case,[{'publication_id':row.id,'require_current':True}]);stale=False
            except (ValueError,OSError,KeyError):stale=True
            state={'id':row.id,'generation':row.generation,'status':row.status,
                'current':active is not None and active.id==row.id,'stale':stale}
        files=[{'path':path,'size':manifest['files'][path]['size'],'sha256':manifest['files'][path]['sha256'],
                'role':'original' if index==0 else 'previous-draft'} for index,path in enumerate(paths)]
        return row,{'family':family(row.kind),'output':output,'publication':state,'files':files}
    except (ValueError,OSError,KeyError,TypeError) as error:
        raise HTTPException(409,{'code':'evidence_invalid','message':'报告产物版本缺失或损坏'}) from error


async def artifact_version(db, case, publication_id, *, composition=False, output=None):
    """Capture the archived version and its successful owned producer under case control."""
    from ..models import Job
    from .artifact_publication import PublicationIdentity, current

    if composition and output is not None and output not in compose_artifacts.OUTPUTS:
        raise HTTPException(400, '报告输出路径不受支持')
    if composition and not publication_id:
        _, kind = compose_artifacts.OUTPUTS[output or 'E-report/report.md']
        _, row = await current(db, case.id, kind)
    else:
        row = await db.get(ArtifactPublication, publication_id, populate_existing=True)
    selected_family = family(row.kind) if row is not None else None
    if row is None or row.case_id != case.id or selected_family is None or (composition and selected_family != 'draft'):
        raise HTTPException(404, '报告产物版本不存在')
    job = await db.scalar(select(Job.id).join(JobAttempt, Job.id == JobAttempt.job_id).where(
        JobAttempt.id == row.attempt_id, Job.case_id == case.id, Job.status == 'succeeded'))
    if row.status not in {'committed', 'superseded'} or job is None:
        raise ValueError('Report derivative has no successful owned Job')
    return PublicationIdentity.capture(row)


@contextmanager
def artifact_reader(prefix, row, path, *, output=None):
    """Authenticate the full bundle and expose only the user's selected output."""
    from .file_read import ArchivedPublication
    from .publication_inputs import _selected_members
    from .step_handlers import kind_for, publication_handler

    with ArchivedPublication(prefix, row) as archive:
        _selected_members(archive.manifest, None)
        descriptor = archive.manifest['descriptor']
        verified = set()
        if family(row.kind) == 'draft':
            if output is not None and descriptor['output'] != output:
                raise HTTPException(404, '报告草稿版本不属于此输出')
            folder = compose_artifacts.directory(descriptor['execution_id'])

            def metadata(name):
                member = folder + '/' + name
                with closing(archive.member(member).chunks()) as blocks:
                    document = json.loads(b''.join(blocks))
                verified.add(member)
                return document

            document = metadata('result.json')
            if not isinstance(document, dict):
                raise ValueError('Composition result must be a JSON object')
            metadata('toolchain.json')
            allowed = {folder + '/report.md'}
            if document.get('backup'):
                allowed.add(document['backup'])
        else:
            handler = publication_handler(descriptor)
            if kind_for(handler, descriptor) != row.kind:
                raise ValueError('Publication channel mismatch')
            allowed = {descriptor['output']}
        if path not in allowed:
            archive.verify_other_than(verified)
            raise HTTPException(404, '所选版本不包含此报告文件')
        archive.verify_other_than(verified | {path})
        yield archive.member(path)
