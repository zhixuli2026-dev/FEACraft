"""Private Codex work trees and conditional case publication."""
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import UUID

from .. import storage
from ..config import get_settings
from . import case_tree, file_transactions as files, sandbox_export_publication as publication
from .case_write import CaseWriteConflict
from .cli_private_paths import root as private_root
from .fs_directory import remove_owned
from .node_storage_budget import reserve
from .request_deadline import current
from .tree_metadata_native import MAX_BYTES


def root(user_id, session_id):
    folder = private_root(get_settings())/'users'/str(user_id)/'sessions'/str(UUID(session_id))/'case-stage'
    if folder.resolve() != folder:
        raise ValueError('Docker CLI stage directory was redirected')
    return folder


def storage_binding(prefix):
    if storage._use_s3():
        from .attempt_objects import binding
        return binding(prefix)
    return {'backend':'fs','prefix':prefix,'root':str(storage._fs_root().resolve())}


def validate(selected):
    value = selected['docker_stage']
    if (not isinstance(value,dict) or set(value) != {'sha256','storage'} or
            not re.fullmatch('[0-9a-f]{64}',value['sha256']) or not isinstance(value['storage'],dict)
            or value['storage'].get('prefix') != selected['source_prefix']):
        raise ValueError('Invalid Docker CLI working copy binding')
    workspace = Path(selected['cwd'])
    if workspace.name != 'files' or workspace.parent.name != 'case-stage':
        raise ValueError('Invalid Docker CLI working copy path')
    return value


def stage_root(selected, session_id):
    binding = validate(selected)
    folder = root(selected['user_id'],session_id)
    if str(folder/'files') != selected['cwd'] or storage_binding(selected['source_prefix']) != binding['storage']:
        raise CaseWriteConflict('input_stale','Docker CLI 暂存存储位置已变化')
    return folder


def encode(value):
    raw = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')
    if len(raw)>MAX_BYTES:
        raise ValueError('Docker CLI working tree metadata is oversized')
    return raw


def prepare(prefix, user_id, session_id):
    folder = root(user_id,session_id)
    before, _ = case_tree.current(prefix)
    binding = storage_binding(prefix)
    raw = encode(before)
    amount = sum(row.get('size',0) for row in before.values())
    with reserve(amount+len(raw),private=True,target_root=folder):
        folder.mkdir(exist_ok=False)
        try:
            target = folder/'files'
            target.mkdir(mode=0o777)
            target.chmod(0o777)
            for name,row in sorted(before.items()):
                if current() is not None:
                    current().check()
                output = files._path(target,name)
                if row['kind']=='directory':
                    output.mkdir(mode=0o777)
                    output.chmod(0o777)
                    continue
                if storage._use_s3():
                    from .s3_streams import snapshot
                    with output.open('xb') as stream:
                        observed = snapshot(prefix,name,output=stream,limit=row['size'],rewind=False)[0]
                        stream.flush()
                        os.fsync(stream.fileno())
                else:
                    observed = files._capture(storage._fs_path(prefix,name),output,limit=row['size'],deadline=current())
                if observed != {key:row[key] for key in ('size','sha256')}:
                    raise CaseWriteConflict('input_stale','Docker CLI 暂存时算例文件变化',path=name)
                output.chmod(0o666 | (row['mode'] & 0o111))
            files._write_new(folder/'baseline.json',raw)
        except BaseException:
            remove_owned(folder,root=folder.parent)
            raise
    return {'sha256':hashlib.sha256(raw).hexdigest(),'storage':binding}


def snapshot(selected, session_id):
    folder = stage_root(selected,session_id)
    baseline = files._path(folder,'baseline.json')
    with baseline.open('rb') as stream:
        raw = stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES or hashlib.sha256(raw).hexdigest() != selected['docker_stage']['sha256']:
        raise ValueError('Docker CLI working copy baseline changed')
    before = case_tree.validate(json.loads(raw))
    after = case_tree.physical(folder/'files')
    if after is None:
        raise ValueError('Docker CLI working copy disappeared')
    after.pop('')
    for name,row in after.items():
        existing = before.get(name)
        if existing and existing['kind']==row['kind']:
            row['mode'] = existing['mode']
        elif os.name=='nt':
            row['mode'] = 0o755 if row['kind']=='directory' else 0o644
    return folder,before,case_tree.validate(after)


async def publish(selected, session_id, turn_id, authorize):
    from .case_write import bounded_case_read
    folder,before,after = await bounded_case_read(snapshot,selected,session_id,
                                                code='codex_stage',label='Codex 算例暂存')
    encoded = encode(after)
    manifest = folder/'manifest.json'
    if manifest.is_file():
        if files._path(folder,'manifest.json').read_bytes()!=encoded:
            raise ValueError('Docker CLI output changed after its first publication attempt')
    else:
        files._write_new(manifest,encoded)
    exporter=SimpleNamespace(root=folder,request_id=turn_id)
    return await publication.publish_tree(exporter,session_id,selected['case_id'],
                selected['source_prefix'],authorize,before,after,schema=publication.CODEX_SCHEMA)


def discard(selected, session_id):
    folder = stage_root(selected,session_id)
    remove_owned(folder,root=folder.parent)


def discard_unmounted(user_id, session_id):
    folder = root(user_id,session_id)
    remove_owned(folder,root=folder.parent)


async def inspect(session_id, user_id, *, path=None):
    from fastapi import HTTPException
    from ..db import SessionLocal
    from . import codex_records, codex_sessions
    from .case_write import bounded_case_read
    from .tree_metadata_native import name

    row = await codex_sessions.row(session_id,user_id)
    selected = json.loads(row.profile_json)
    if 'docker_stage' not in selected:
        raise HTTPException(404,'此会话没有容器算例暂存')
    async with SessionLocal() as db:
        await codex_sessions.authorize(db,row.case_id,row.conversation_id,user_id)
    journal = codex_records.public(row)
    if journal['phase'] not in {'closed','failed'} or journal['cleanup_verified'] is not True:
        raise HTTPException(409,'容器退出未核验，暂存文件尚不可查看')

    def read():
        folder = stage_root(selected,session_id)
        if not folder.exists():
            if path is not None:
                raise HTTPException(404,'暂存文件已清理')
            return {'retained':False,'changes':[]}
        folder,before,after = snapshot(selected,session_id)
        if path is None:
            changes = [{'path':item,'before':before.get(item),'after':after.get(item)}
                       for item in sorted(before.keys() | after.keys()) if before.get(item) != after.get(item)]
            return {'retained':True,'changes':changes}
        name(path)
        record = after.get(path)
        if record is None or record['kind'] != 'file':
            raise HTTPException(404,'暂存文件不存在')
        if record['size']>256*1024:
            raise HTTPException(413,'文本预览最多 256 KiB')
        with files._path(folder/'files',path).open('rb') as stream:
            content = stream.read(record['size']+1)
        if hashlib.sha256(content).hexdigest()!=record['sha256']:
            raise ValueError('Docker CLI staged file changed')
        try:
            text = content.decode('utf8')
        except UnicodeError as error:
            raise HTTPException(422,'暂存文件无法作为文本预览') from error
        if '\0' in text:
            raise HTTPException(422,'暂存文件无法作为文本预览')
        return {'path':path,'content':text,'sha256':record['sha256']}

    return await bounded_case_read(read,code='codex_stage',label='Codex 算例暂存')
