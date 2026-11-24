"""Complete user-file snapshots, native deltas and publication freshness."""
from pathlib import PurePosixPath
import hashlib
import json

from .. import storage
from . import attempt_workspace as workspace


def restore_event_prefix(prefix,descriptor,observed):
    """Retain frozen history while accepting only intact platform appends."""
    from .case_events import CASE_EVENTS_PATH
    previous=descriptor['source']['files'].get(CASE_EVENTS_PATH)
    current=observed['files'].get(CASE_EVENTS_PATH)
    if current is None or current==previous:return observed
    data=storage.get_bytes(prefix,CASE_EVENTS_PATH)
    if len(data)!=current['size'] or hashlib.sha256(data).hexdigest()!=current['sha256']:
        raise ValueError('Case timeline changed during source inspection')
    size=previous['size'] if previous else 0
    if (len(data)<=size or not data.endswith(b'\n') or
            (size and (not data[:size].endswith(b'\n') or hashlib.sha256(data[:size]).hexdigest()!=previous['sha256'])) or
            (previous is not None and previous['executable']!=current['executable'])):
        return observed
    try:
        events=[json.loads(line) for line in data[size:].decode('utf8').splitlines()]
        if not events or any(not isinstance(event,dict) or event.get('schema')!='vibeflow.case-event.v1'
                or type(event.get('case_id')) is not int or event['case_id']!=descriptor['case_id'] for event in events):return observed
    except (UnicodeError,ValueError,TypeError):return observed
    if previous is None:observed['files'].pop(CASE_EVENTS_PATH,None)
    else:observed['files'][CASE_EVENTS_PATH]=previous
    return observed


def snapshot(root):
    directories=[];paths=workspace.live_roots(root)
    files=workspace.inventory(root,paths,directories) if paths else {}
    return {'files':files,'directories':directories}


def file_records(source):
    return {p:{k:r[k] for k in ('size','sha256')} for p,r in source['files'].items()}


def parents(paths):
    result=set()
    for path in paths:
        result.update(p.as_posix() for p in PurePosixPath(path).parents if p.as_posix()!='.')
    return result


def changes(root,source,stamps):
    after=snapshot(root)
    from .case_events import CASE_EVENTS_PATH
    if after['files'].get(CASE_EVENTS_PATH)!=source['files'].get(CASE_EVENTS_PATH):
        raise ValueError('Native task changed the preserved platform timeline')
    outputs={p:r for p,r in after['files'].items() if p!=CASE_EVENTS_PATH and (source['files'].get(p)!=r
        or workspace.checked_path(root,p).stat().st_mtime_ns!=stamps[p])}
    deleted={p:r for p,r in source['files'].items() if p not in after['files']}
    return outputs,deleted


def current_source(root,descriptor):
    return restore_source(snapshot(root),descriptor)


def restore_source(observed,descriptor):
    from .case_events import CASE_EVENTS_PATH
    original=descriptor['source']
    for path,after in descriptor.get('output_files',{}).items():
        if path==CASE_EVENTS_PATH:continue
        if observed['files'].get(path)==after:
            if path in original['files']:observed['files'][path]=original['files'][path]
            else:observed['files'].pop(path,None)
    for path in descriptor.get('deleted_files',{}):
        if path==CASE_EVENTS_PATH:continue
        if path not in observed['files']:observed['files'][path]=original['files'][path]
    added_dirs=parents(p for p in descriptor.get('output_files',{}) if p!=CASE_EVENTS_PATH)-set(original['directories'])
    observed['directories']=[p for p in observed['directories'] if p not in added_dirs]
    observed['files']=dict(sorted(observed['files'].items()))
    return observed


