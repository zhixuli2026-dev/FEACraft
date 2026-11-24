"""Native initializer node claims survive the API owner until durable proof."""
import hashlib,json,os,re,stat,time,uuid
from pathlib import Path
from .. import storage
from ..config import get_settings
from . import node_storage_budget as budget,process_identity_native as identity
from .storage_usage import Counter,_ordinary
from .durable_events import json_text

SCHEMA='vibeflow.cli-node-capacity.v1'
FRAGMENT=re.compile(r'([0-9a-f]{32})\.([0-9a-f]{64})\.cli-(pending|retired-pending|retired-published)')


def _present(path):
    try:path.lstat();return True
    except FileNotFoundError:return False


def fragment(path,info):
    """Recognize bounded metadata; incomplete pending files reserve the maximum."""
    match=FRAGMENT.fullmatch(path.name)
    if match is None:return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>16384:
        raise ValueError('Invalid native node reservation fragment')
    return {'session_id':str(uuid.UUID(match[1])),'pending':match[3]=='pending','reserved_bytes':32768 if match[3]=='pending' else 0}


def _read(path):
    info=_ordinary(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>16384:
        raise ValueError('Invalid native node reservation metadata')
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno());data=stream.read(16385)
    after=_ordinary(path)
    fields=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_nlink)
    if fields(info)!=fields(opened) or fields(info)!=fields(after) or len(data)!=info.st_size:
        raise ValueError('Native node reservation changed during reading')
    return data


def _paths(directory,identifier,data):
    prefix=identifier+'.'+hashlib.sha256(data).hexdigest()+'.cli-'
    return directory/(identifier+'.json'),directory/(prefix+'pending'),prefix


def validate(row,root,identifier):
    if (set(row)!={'schema','id','root','owner','bytes','session_id','user_id','selection_sha256'}
            or row['schema']!=SCHEMA or row['id']!=identifier or row['root']!=str(root)
            or str(uuid.UUID(row['session_id']))!=row['session_id'] or uuid.UUID(row['session_id']).hex!=identifier
            or type(row['bytes']) is not int or not 0<=row['bytes']<=32768
            or type(row['user_id']) is not int or row['user_id']<=0
            or not re.fullmatch('[0-9a-f]{64}',row['selection_sha256'])):
        raise ValueError('Invalid native node reservation')
    identity.validate(row['owner'])
    return row


def encoded(ticket):
    root=storage._fs_root().resolve()
    if ticket['node_root']!=str(root):raise ValueError('Native node storage binding changed')
    identifier=uuid.UUID(ticket['session_id']).hex
    row={'schema':SCHEMA,'id':identifier,'root':str(root),'owner':ticket['owner'],
        'bytes':ticket['bytes'],'session_id':ticket['session_id'],'user_id':ticket['user_id'],
        'selection_sha256':ticket['selection_sha256']}
    validate(row,root,identifier)
    data=json_text(row).encode('utf8')
    if len(data)>16384:raise ValueError('Native node reservation exceeds metadata bound')
    return root,identifier,data


def acquire(ticket):
    from .file_transactions import _write_new,_sync
    from .request_deadline import current
    root,identifier,data=encoded(ticket);root.mkdir(parents=True,exist_ok=True)
    settings=get_settings();deadline=time.monotonic()+settings.storage_measurement_timeout_seconds
    parent=current()
    if parent is not None:deadline=min(deadline,parent.deadline)
    counter=Counter(settings,deadline,None)
    with budget._locked(root,deadline) as directory:
        path,pending,_=_paths(directory,identifier,data)
        if _present(pending):raise ValueError('Native node pending reservation requires reconciliation')
        if _present(path):
            if _read(path)!=data:
                raise ValueError('Native node reservation already has another binding')
            budget._capacity(settings,root,directory,counter,0,private=True)
        else:
            budget._capacity(settings,root,directory,counter,ticket['bytes']+len(data),private=True)
            _write_new(pending,data)
            # The shared OS lock excludes every cooperating publisher. A CLI
            # cannot be created until this atomic publication has completed.
            if _present(path):raise ValueError('Native node publication target changed')
            os.rename(pending,path)
            _sync(directory)


def release(ticket,*,allow_fragments=False):
    from .file_transactions import _sync
    if type(allow_fragments) is not bool:raise ValueError('Invalid fragment reconciliation selection')
    root,identifier,data=encoded(ticket)
    deadline=time.monotonic()+get_settings().storage_measurement_timeout_seconds
    with budget._locked(root,deadline) as directory:
        path,pending,prefix=_paths(directory,identifier,data)
        for candidate,kind in ((path,'published'),(pending,'pending')):
            try:actual=_read(candidate)
            except FileNotFoundError:continue
            if candidate==path and actual==data:
                path.unlink();_sync(directory);continue
            if not allow_fragments or not data.startswith(actual):
                raise ValueError('Native node reservation changed before settlement')
            retired=directory/(prefix+'retired-'+kind)
            if _present(retired):
                # Never replace an earlier fragment. Identical duplicates can
                # only be removed after proving their exact preserved bytes.
                if _read(retired)!=actual:raise ValueError('Native node fragment history changed')
                candidate.unlink()
            else:os.rename(candidate,retired)
            _sync(directory)


def outstanding(ticket):
    root,identifier,_=encoded(ticket)
    deadline=time.monotonic()+get_settings().storage_measurement_timeout_seconds
    with budget._locked(root,deadline) as directory:
        try:(directory/(identifier+'.json')).lstat();return True
        except FileNotFoundError:pass
        counter=Counter(get_settings(),deadline,None)
        for path in directory.iterdir():
            counter.check();counter.add(0)
            if path.name.startswith(identifier+'.') and path.name.endswith('.cli-pending'):return True
        return False


def pending(user_id):
    settings=get_settings();root=storage._fs_root().resolve()
    root.mkdir(parents=True,exist_ok=True)
    counter=Counter(settings,time.monotonic()+settings.storage_measurement_timeout_seconds,None)
    found=[]
    with budget._locked(root,counter.deadline) as directory:
        for path in directory.iterdir():
            counter.check();counter.add(0)
            if path.name=='lock':continue
            info=_ordinary(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>16384:
                raise ValueError('Invalid node reservation inventory')
            piece=fragment(path,info)
            if piece is not None:
                if piece['pending'] and piece['session_id'] not in found:found.append(piece['session_id'])
                if len(found)>=100:break
                continue
            row=json.loads(path.read_bytes())
            if row.get('schema')!=SCHEMA:continue
            validate(row,root,path.stem)
            if row['user_id']==user_id:found.append(row['session_id'])
            if len(found)>=100:break
    return found
