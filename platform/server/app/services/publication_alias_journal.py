"""Hash-conditional compatibility copies backed by an immutable operation plan."""
import hashlib
import os
import uuid

from .. import storage
from . import publication_journal as journal
from . import attempt_workspace as workspace
from .compose_artifacts import existing,record
from .durable_events import json_text

SCHEMA='vibeflow.publication-alias-plan.v1'


def _hash(data):
    return record(data) if data is not None else None


def operations(manifest):
    descriptor=manifest['descriptor'];files=manifest['files']
    retired=descriptor.get('retired_aliases',{})
    if descriptor.get('handler') in {'workflow-mpi.v1','workflow-udf.v1','workflow-udf-build.v1'}:
        native=descriptor['native'];origin=native.get('origin_source',native['source'])
        before={p:{k:r[k] for k in ('size','sha256')} for p,r in origin['files'].items()}
        removed=set(descriptor['deleted_files'])-set(files)-set(retired)
        return ([{'path':p,'source':None,'allowed_before':[None,{k:v[k] for k in ('size','sha256')}],
                  'preserve_conflict':True} for p,v in retired.items() if p not in files]+
            [{'path':p,'source':None,'allowed_before':[before[p]]} for p in sorted(removed)]+
            [{'path':p,'source':p,'allowed_before':[before.get(p)]} for p in files])
    if descriptor.get('handler')=='postprocess-case.v1' and 'targets_before' in descriptor:
        return [{'path':p,'source':p,'allowed_before':[descriptor['targets_before'].get(p)]} for p in files]
    if descriptor.get('handler') in {'prepared-solve.v1','paraview.v1','mpi.v1','udf.v1','flotherm-convert.v1','flotherm-library.v1','case-command.v1'}:
        origin=descriptor.get('origin_source',descriptor['source'])
        before={p:{k:r[k] for k in ('size','sha256')} for p,r in origin['files'].items()}
        return ([{'path':p,'source':None,'allowed_before':[before[p]]} for p in descriptor['deleted_files']]+
            [{'path':p,'source':p,'allowed_before':[before.get(p)]} for p in files])
    if descriptor.get('handler') in {'calculix.v1','su2.v1','elmer.v1','moose.v1','code-aster.v1'}:
        return ([{'path':p,'source':None,'allowed_before':[None,{k:v[k] for k in ('size','sha256')}]} for p,v in retired.items()]+
            [{'path':p,'source':p,'allowed_before':[descriptor['targets_before'].get(p)]} for p in files])
    if descriptor.get('handler') in {'pdf-number.v1','ppt.v1','ppt-media.v1'}:
        return [{'path':descriptor['output'],'source':descriptor['output'],'allowed_before':[None]}]
    if descriptor.get('handler')=='report-compose.v1':
        from .compose_artifacts import directory
        source=directory(descriptor['execution_id'])+'/report.md'
        return ([{'path':p,'source':p,'allowed_before':[None]} for p in files]+
            [{'path':descriptor['output'],'source':source,'allowed_before':[descriptor['target_before']]}])
    if descriptor.get('handler')=='latex.v1':
        from .latex_artifacts import paths
        names=paths(descriptor['main'])
        selected=[p for p in names.values() if p in files and p!=names['pdf']]+[names['pdf']]
    else:
        selected=list(files)
    if set(selected)&set(retired):raise ValueError('Retired alias is also a current output')
    return ([{'path':p,'source':None,'allowed_before':[None,{k:v[k] for k in ('size','sha256')}],
        **({'preserve_conflict':True} if descriptor.get('handler') in {'solve.v1','solve.resume.v1'} else {})} for p,v in retired.items()]+
        [{'path':p,'source':p} for p in selected])


def preserves_retired_edit(operation):
    """Only signed solve retirement deletions may leave an edited old output."""
    return (operation.get('preserve_conflict') is True
        and operation['source'] is None and operation['after'] is None)


def prepare(prefix,row,manifest):
    """Called under the renewable case content fence, before terminal DB commit."""
    if storage._use_s3():
        from .publication_alias_objects import prepare as remote_prepare
        return remote_prepare(prefix,row,manifest)
    from .artifact_publication import read_file
    root,directory=journal._directory(prefix,row)
    if not directory.exists():raise ValueError('Publication has no durable bundle intent')
    plan=[]
    for operation in operations(manifest):
        path=workspace.canonical_path(operation['path'])
        workspace.assert_mutable_path(prefix,path)
        before=_hash(existing(root,path))
        source=operation['source']
        after=_hash(read_file(prefix,row,source)) if source else None
        if 'allowed_before' in operation and before!=after and before not in operation['allowed_before']:
            # Keep the original write precondition, never bless edited bytes as
            # a new deletion/overwrite baseline. Alias conflicts remain optional
            # repair errors; the immutable result can still be committed.
            before=operation['allowed_before'][-1]
        plan.append({'path':path,'source':source,'before':before,'after':after,
            **({'preserve_conflict':True} if operation.get('preserve_conflict') is True else {})})
    value={'schema':SCHEMA,'publication_id':row.id,'case_id':row.case_id,'attempt_id':row.attempt_id,
        'manifest_sha256':row.manifest_sha256,'operations':plan}
    encoded=json_text(value).encode('utf8');digest=hashlib.sha256(encoded).hexdigest()
    with journal._lock(directory):
        path=directory/'aliases.json'
        if path.exists():
            if journal._read(path)!=value:raise ValueError('Alias plan changed after preparation')
        else:
            temporary=directory/('.aliases-'+str(uuid.uuid4()))
            try:
                journal._write_new(temporary,value);os.replace(temporary,path);journal._flush_directory(directory)
            finally:temporary.unlink(missing_ok=True)
    return digest


def read_plan(directory,row,digest):
    value=journal._read(directory/'aliases.json')
    if (hashlib.sha256(json_text(value).encode('utf8')).hexdigest()!=digest or value.get('schema')!=SCHEMA
            or value.get('publication_id')!=row.id or value.get('case_id')!=row.case_id
            or value.get('attempt_id')!=row.attempt_id or value.get('manifest_sha256')!=row.manifest_sha256):
        raise ValueError('Alias plan no longer matches its committed publication')
    return value


def record_applied(directory,digest):
    receipt={'schema':SCHEMA,'plan_sha256':digest,'state':'applied'}
    path=directory/'aliases-applied.json'
    if path.exists():
        if journal._read(path)!=receipt:raise ValueError('Alias application receipt differs')
    else:
        temporary=directory/('.applied-'+str(uuid.uuid4()))
        try:
            journal._write_new(temporary,receipt);os.replace(temporary,path);journal._flush_directory(directory)
        finally:temporary.unlink(missing_ok=True)


def apply(prefix,row,digest):
    """Caller holds case control and verified that this head is still active."""
    if storage._use_s3():
        from .publication_alias_objects import apply as remote_apply
        return remote_apply(prefix,row,digest)
    from .artifact_publication import read_file
    root,directory=journal._directory(prefix,row)
    with journal._lock(directory):
        value=read_plan(directory,row,digest)
        # Current output conflicts still reject the whole update. Solve's old
        # time directories retire independently; preserve edits, but do not let
        # one edited old field prevent unrelated obsolete fields being removed.
        preserved=set()
        for operation in value['operations']:
            path=workspace.canonical_path(operation['path']);workspace.assert_mutable_path(prefix,path)
            present=_hash(existing(root,path))
            if present not in (operation['before'],operation['after']):
                if preserves_retired_edit(operation):
                    preserved.add(path)
                    continue
                raise ValueError('Edited compatibility destination was preserved: '+path)
        for operation in value['operations']:
            if operation['path'] in preserved:continue
            path=operation['path'];present=_hash(existing(root,path))
            if present==operation['after']:continue
            if present!=operation['before']:
                if preserves_retired_edit(operation):
                    preserved.add(path)
                    continue
                raise ValueError('Compatibility destination changed: '+path)
            if operation['source'] is None:
                workspace.checked_path(root,path).unlink()
            else:
                data=read_file(prefix,row,operation['source'])
                if _hash(data)!=operation['after']:raise ValueError('Alias source no longer matches')
                storage._fs_put(prefix,path,data)
        if preserved:
            raise ValueError('Edited retired solve outputs were preserved: '+', '.join(sorted(preserved)))
        record_applied(directory,digest)
