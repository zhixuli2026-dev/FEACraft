"""Real local subtree recovery at owner and node quota boundaries on both databases."""
import asyncio
import hashlib
import json

from live_llm_job_repeat_acceptance import main, put


FIXTURE = '''
import hashlib,json
from pathlib import Path
from fastapi import Depends
from app.main import app
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import case_tree, tree_transactions as trees
from app.services.case_write import coordinate
from app.services.durable_events import json_text
from app.services.quota import ensure_free_plan, quotas_for
from app.services.storage_usage import measure_user
root=Path(__file__).parent
MIB=1024*1024

@app.post('/api/v1/fixture/tree-budget/{cid}/{decision}')
async def prepare(cid:int,decision:str,user=Depends(get_current_user),db=Depends(get_db)):
    assert decision in ('rolled_back','committed') and not storage._use_s3()
    case=await get_owned_case(cid,user,db,write=True)
    base='RB12-'+decision
    snap=root/('snapshot-'+decision)
    snap.mkdir()
    async with coordinate(db,case):
        present,raw=case_tree.current(case.s3_prefix)
        target=dict(present)
        assert target[base+'/seed']['kind']=='file'
        for subdir in ('seed','empty'):
            target[base+'/'+subdir]={'kind':'directory','mode':0o750}
        for i in range(128):
            path=base+'/seed/'+str(i)+'.bin'
            data=hashlib.sha256(str(i).encode()).digest()*64
            file=snap/path
            file.parent.mkdir(exist_ok=True,parents=True)
            file.write_bytes(data)
            target[path]={'kind':'file','mode':0o640,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
        prefix=case.s3_prefix
        tx=await trees.write(db,case,present,target,snap,raw)
        pending=trees._base(case)/'pending'
        document=json.loads((pending/'manifest.json').read_text('utf8'))
        receipt={'transaction_id':tx,'decision':decision,'event_id':document['event_id'],
                 'plan_sha256':document['plan_sha256']}
    if decision=='committed':
        await db.commit()
    else:
        await db.rollback()
    return {'prefix':prefix,'tx':tx,'receipt':receipt,'bytes':len(json_text(receipt).encode())}

@app.post('/api/v1/fixture/tree-room/{cid}/{room}')
async def configure(cid:int,room:int,node_full:bool=False,user=Depends(get_current_user),db=Depends(get_db)):
    case=await get_owned_case(cid,user,db,write=True)
    assert not storage._use_s3() and -1<=room<=16384
    folder=storage._fs_root()/'workspaces'/str(user.id)
    padding=folder/'.tree-budget-test-padding'
    padding.unlink(missing_ok=True)
    storage_root=storage._fs_root()
    node_padding=storage_root/'.tree-budget-node-padding'
    node_padding.unlink(missing_ok=True)
    plan=await ensure_free_plan(db)
    quotas=json.loads(plan.quotas)
    measured=(await measure_user(user.id)).bytes
    if room<0:
        quotas['storage_mb']=2048
    else:
        quotas['storage_mb']=(measured+room)//MIB+1
        with padding.open('xb') as stream:
            stream.truncate(quotas['storage_mb']*MIB-measured-room)
    if node_full:
        with node_padding.open('xb') as stream:
            stream.truncate(2*MIB)
    get_settings().node_storage_limit_mb=1 if node_full else 10240
    plan.quotas=json.dumps(quotas)
    await db.flush()
    assert (await quotas_for(db,user))['storage_mb']==quotas['storage_mb']
    await db.commit()
    return {'free':quotas['storage_mb']*MIB-(await measure_user(user.id)).bytes}
'''


def inventory(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in root.rglob('*')}


def scenario(api, *, store=None, **_):
    assert store is None
    observations = []
    for decision in ('rolled_back','committed'):
        base = 'RB12-'+decision
        put(api,base+'/seed','original')
        info = api.call('POST',f'/fixture/tree-budget/{api.case_id}/{decision}')
        root = api.root/'files'/info['prefix']
        base_dir = root/base
        journal = root/'.vibeflow/tree-transactions'
        pending = journal/'pending'
        live_before, staged_before = inventory(base_dir), inventory(pending)
        assert len(list((base_dir/'seed').glob('*.bin'))) == 128
        size = info['bytes']
        allowance = f'/fixture/tree-room/{api.case_id}/'
        recover = api.prefix+'/file-transactions:recover'
        for room, node_full, status in ((size-1,False,429),(size,True,409)):
            query = '?node_full=true' if node_full else ''
            assert api.call('POST',allowance+str(room)+query)['free'] == room
            rejected = api.call('POST',recover,expect=status)
            if node_full:
                assert rejected['detail']['code']=='node_storage_full'
            assert inventory(base_dir)==live_before and inventory(pending)==staged_before
        assert api.call('POST',allowance+str(size))['free']==size
        api.call('POST',recover)
        history = journal/'history'/info['tx']
        resolution, = history.glob('resolution-*.json')
        assert resolution.stat().st_size == size and json.loads(resolution.read_text())==info['receipt']
        assert not pending.exists()
        if decision=='rolled_back':
            assert (base_dir/'seed').read_text()=='original' and not (base_dir/'empty').exists()
        else:
            assert inventory(base_dir)==live_before and (base_dir/'empty').is_dir()
            entries=json.loads((root/'.vibeflow/tree-state.json').read_text())['entries']
            assert entries[base+'/empty']['mode']==0o750
            assert all(entries[base+'/seed/'+str(i)+'.bin']['mode']==0o640 for i in range(128))
        api.call('POST',recover)
        assert list(history.glob('resolution-*.json'))==[resolution]
        api.call('POST',allowance+'-1')
        observations.append(dict(decision=decision, files=128, receipt_bytes=size,
                                 owner_denied_at=size-1,node_denied=True,retained_hashes_unchanged=True,
                                 admitted_at=size,duplicate_receipts=0))
    report={'db':'postgres' if api.database_url else 'sqlite','storage':'fs','observations':observations}
    (api.root/'tree-budget.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
