"""Exercise multi-file S3 tree publication and recovery with real local and remote data."""
import asyncio
import hashlib
import json
from pathlib import Path

import httpx

from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
import gc, hashlib, json, os, tracemalloc
from contextlib import contextmanager
from pathlib import Path
from fastapi import Depends
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.main import app
from app.routers.workspaces import get_owned_case
from app.services import case_tree, file_transactions, s3_file_transactions as remote
from app.services import tree_transactions, tree_transactions_s3 as tree
from app.services.case_write_capacity import committing
from app.services.s3_live_store import Client
root = Path(__file__).parent
snap = root/'incoming-tree'
measure = tree._changes
original_copy = remote._temporary_copy
original_prepare = remote._prepare_after
original_apply = remote._apply
original_put = Client.put_object

def observed_changes(*args):
    gc.collect()
    tracemalloc.start()
    try:
        changes, expected = measure(*args)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    refs = [v for v in changes.values() if isinstance(v,remote.TreeSnapshotFile)]
    (root/'tree-preflight.json').write_text(json.dumps(dict(peak=peak, sources=len(refs),
        bytes=sum(v.expected['size'] for v in refs))))
    return changes,expected

@contextmanager
def observed_copy(size):
    with original_copy(size) as stream:
        base = storage._fs_root().resolve()
        assert Path(stream.name).resolve().parent == base
        claims = [json.loads(p.read_text()) for p in (base/'.node-storage-budget').glob('*.json')]
        assert any(r['owner']['pid']==os.getpid() and r['bytes']==size for r in claims)
        try:
            yield stream
        finally:
            actual=os.fstat(stream.fileno()).st_size
            assert actual <= size
            with (root/'copy-sizes.jsonl').open('a') as log:
                log.write(json.dumps([size,actual])+chr(10))

def observed_prepare(prefix, path, data, metadata):
    if (root/'replace-verified').exists() and isinstance(data,remote.TreeSnapshotFile) and data.relative.endswith('/01.bin'):
        with (data.root/data.relative).open('r+b') as file:
            file.write(b'changed!')
        (root/'replace-verified').unlink()
    return original_prepare(prefix,path,data,metadata)

def observed_put(self,**args):
    result=original_put(self,**args)
    flag=root/'grow-during-upload'
    if flag.exists() and '/after/' in args['Key'] and args['Key'].endswith('/01.bin'):
        with (snap/'Ref/01.bin').open('ab') as source:
            source.write(b'!')
        flag.unlink()
    return result

def observed_apply(case, directory, index, entry, side, expected_side):
    settings=get_settings()
    limit=settings.node_storage_limit_mb
    if (root/'deny-second').exists() and side=='after' and entry['path'].endswith('/01.bin'):
        settings.node_storage_limit_mb=1
    try:
        return original_apply(case,directory,index,entry,side,expected_side)
    finally:
        settings.node_storage_limit_mb=limit

tree._changes=observed_changes
remote._temporary_copy=observed_copy
remote._prepare_after=observed_prepare
remote._apply=observed_apply
Client.put_object=observed_put

@app.post('/api/v1/fixture/restore-tree/{cid}')
async def restore_tree(cid:int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(cid,user,db,write=True)
    assert storage._use_s3()
    async with committing(db,case):
        present,raw=await file_transactions._io(case_tree.current,case.s3_prefix)
        target=dict(present)
        target['Ref/empty-restored']={'kind':'directory','mode':0o751}
        for path,record in json.loads((root/'expected-files.json').read_text()).items():
            target[path]={'kind':'file','mode':0o640,**record}
        tx=await tree_transactions.write(db,case,present,target,snap,raw)
    return {'id':tx}

@app.get('/api/v1/fixture/observe-tree/{cid}')
async def observe_tree(cid:int,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(cid,user,db,write=False)
    present,_=await file_transactions._io(case_tree.current,case.s3_prefix)
    return {k:v for k,v in present.items() if k.startswith('Ref/') and
            (k.endswith('.bin') or k.endswith('empty-restored'))}
'''


def scenario(api, *, store=None, **_):
    assert store is not None
    snap=api.root/'incoming-tree'
    (snap/'Ref').mkdir(parents=True)
    expected={}
    for i in range(9):
        key=f'Ref/{i:02}.bin'
        block=hashlib.sha256(key.encode()).digest()*2048
        digest=hashlib.sha256()
        with (snap/key).open('xb') as file:
            for _ in range(48 if i<8 else 0):
                file.write(block)
                digest.update(block)
        expected[key]={'size':(snap/key).stat().st_size,'sha256':digest.hexdigest()}
    (api.root/'expected-files.json').write_text(json.dumps(expected))
    original='retained prior version'
    put(api,'Ref/00.bin',original)
    observe=f'/fixture/observe-tree/{api.case_id}'
    restore=f'/fixture/restore-tree/{api.case_id}'
    inspect=api.prefix+'/file-transactions'
    before=api.call('GET',observe)
    def claims():
        return list((api.root/'files'/'.node-storage-budget').glob('*.json'))
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      trust_env=False,timeout=180) as client:
        def submit():
            return client.post(restore)
        source=snap/'Ref/01.bin'
        with source.open('rb') as file:
            prefix=file.read(8)
        def reset():
            with source.open('r+b') as file:
                file.write(prefix)
                file.truncate(expected['Ref/01.bin']['size'])
        for stage in ('preflight','publication','during_upload'):
            if stage=='preflight':
                with source.open('r+b') as file:
                    file.write(b'changed!')
            elif stage=='publication':
                (api.root/'replace-verified').touch()
            else:
                (api.root/'grow-during-upload').touch()
            stale=submit()
            assert stale.status_code==409 and stale.json()['detail']['code']=='recovery_evidence_invalid',stale.text[:500]
            assert api.call('GET',inspect)['status']=='idle' and api.call('GET',observe)==before and not claims()
            reset()
        deny=api.root/'deny-second'
        deny.touch()
        try:
            refused=submit()
            assert refused.status_code==409 and refused.json()['detail']['code']=='node_storage_full',refused.text[:500]
        finally:
            deny.unlink()
        pending=api.call('GET',inspect)
        assert pending['status']=='pending' and not pending['database_committed'] and not claims()
        partial=api.call('GET',observe)
        assert partial['Ref/00.bin']['sha256']==expected['Ref/00.bin']['sha256'] and 'Ref/01.bin' not in partial
        retained=api.root/'retained-tree'
        assert snap.parent==retained.parent==api.root and not retained.exists()
        snap.rename(retained)
        try:
            recovered=client.post(api.prefix+'/file-transactions:recover')
            assert recovered.status_code==200,recovered.text[:500]
            assert recovered.json()['result']['decision']=='rolled_back'
            assert api.call('GET',observe)==before and not claims()
        finally:
            retained.rename(snap)
        success=submit()
        assert success.status_code==200 and success.json()['id'],success.text[:500]
        after=api.call('GET',observe)
        assert after['Ref/empty-restored']['mode']==0o751
        for key,record in expected.items():
            assert after[key]=={'kind':'file','mode':0o640,**record}
            with client.stream('GET',api.prefix+'/raw',params={'path':key}) as response:
                assert response.status_code==200
                h=hashlib.sha256()
                for chunk in response.iter_bytes():
                    h.update(chunk)
                assert h.hexdigest()==record['sha256']
        assert api.call('GET',inspect)['status']=='idle' and not claims()
    memory=json.loads((api.root/'tree-preflight.json').read_text())
    assert memory['sources']==9 and memory['bytes']==24*1024*1024 and memory['peak']<5*1024*1024
    copies=[json.loads(line) for line in (api.root/'copy-sizes.jsonl').read_text().splitlines()]
    assert all(actual<=reserved for reserved,actual in copies)
    assert max(actual for _,actual in copies)==3*1024*1024
    report=dict(database='postgres' if api.database_url else 'sqlite',storage='s3',preflight=memory,
                largest_temporary_copy_bytes=max(s for s,_ in copies),observed_copies=len(copies),
                corrupt_snapshot_rejected_before_journal=True,replaced_source_rejected_at_publication=True,
                grew_during_upload_refused=True,second_install_denied=True,remote_rollback_without_local_snapshot=True,
                recovered_then_retried=True,verified_files=len(expected),final_claims=0)
    (api.root/'s3-tree-capacity.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=6))
