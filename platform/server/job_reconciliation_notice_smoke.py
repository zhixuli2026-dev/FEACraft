"""Durable per-Job decisions and lock-free owner inspection on disposable APIs.

Boundary phases are explicit database fixtures, not simulated solver crashes.
The sentinel birth is real and retained until its exit has been observed.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from job_owner_startup_smoke import FIXTURE as OWNER, shared
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity

FIXTURE = OWNER + '''
from pathlib import Path
import time
from app.models import EventOutbox
from app.services import job_recovery_notice as notices,job_worker_identity
ROOT=Path(__file__).parent
original_inspect=job_worker_identity.inspect
def held_inspect(job,attempt):
 result=original_inspect(job,attempt)
 if (ROOT/('unavailable-'+str(job.id))).exists():return {'state':'unverified','reason':'identity_unavailable'}
 marker=ROOT/('hold-'+str(job.id))
 if marker.exists():
  (ROOT/('observed-'+str(job.id))).write_text('ready')
  deadline=time.monotonic()+20
  while not (ROOT/('continue-'+str(job.id))).exists() and time.monotonic()<deadline:time.sleep(.02)
  if not (ROOT/('continue-'+str(job.id))).exists():raise RuntimeError('Fixture identity hold timed out')
 return result
job_worker_identity.inspect=held_inspect
@app.post('/api/v1/fixture/reconcile-one/{job_id}')
async def reconcile_one(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  j=await db.get(Job,job_id);await get_owned_case(j.case_id,user,db)
 return {'result':await job_recovery.reconcile_active_job(job_id)}
@app.get('/api/v1/fixture/proof/{job_id}')
async def proof(job_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  records=await notices.rows(db,job_id);_,j,a,_=records
  await get_owned_case(j.case_id,user,db)
  events=[]
  for e in await db.scalars(select(DurableEvent).where(DurableEvent.case_id==j.case_id,DurableEvent.type.in_((notices.EVENT,notices.RESOLVED))).order_by(DurableEvent.seq)):
   value=json.loads(e.payload_json)
   if value['job_id']==job_id:
    outbox=await db.scalar(select(EventOutbox).where(EventOutbox.event_id==e.id))
    events.append({'id':e.id,'type':e.type,'payload':value,'outbox':outbox is not None})
  return {'snapshot':notices.fingerprint(records),'events':events,'recovery':await notices.public(db,j)}
@app.post('/api/v1/fixture/change/{job_id}')
async def change(job_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  records=await notices.rows(db,job_id,lock=True);_,j,a,head=records
  await get_owned_case(j.case_id,user,db)
  if data['mode']=='heartbeat':
   a.heartbeat_at=now();a.lease_expires_at=now()+timedelta(seconds=12)
  elif data['mode']=='lease_future':a.lease_expires_at=now()+timedelta(days=365)
  elif data['mode']=='output':a.output_manifest_json='{"fixture_changed":true}'
  elif data['mode']=='lineage':head.active_attempt_id=None
  else:raise ValueError('Unknown fixture change')
  await db.commit()
 return {'ok':True}
'''


def api(label, url=None, overrides=None):
    server=IsolatedServer(label,app='notice_fixture:app',database_url=url)
    (server.root/'notice_fixture.py').write_text(FIXTURE.replace('OVERRIDES',repr(overrides or {})),encoding='utf8')
    return server


def verify(url=None):
    checks=[];receipts=[]
    def passed(name):
        checks.append(name);print('PASS '+name,flush=True)
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-u','-c',
        'import sys;print("ready",flush=True);sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    assert sentinel.stdout.readline().strip()==b'ready'
    birth=identity.observe(sentinel.pid)
    sentinel.communicate(b'x',timeout=10)
    assert identity.probe(birth)=={'state':'exited','reason':'process_exited'}
    owner=api('reconciliation-owner',url)
    with owner:
        owner.create_case('Recovery decisions')
        labels={'unclaimed':'input_recovery_required','legacy':'legacy_unverified',
            'legacy_claim':'owner_unverified','malformed':'owner_unverified','foreign_host':'owner_unverified',
            'birth_mismatch':'owner_unverified','cross_attempt':'owner_unverified',
            'finalizing_dead':'publication_recovery_required','claimed_live_expired':None}
        def seed(label):
            return owner.call('POST',f'/fixture/seed/{owner.case_id}',{'label':label,'dead_birth':birth})['id']
        ids={label:seed(label) for label in labels}
        before={label:owner.call('GET',f'/fixture/proof/{job}') for label,job in ids.items()}
        peer=api('reconciliation-peer',url,shared(owner))
        with peer:
            peer.token=owner.token
            for label,reason in labels.items():
                job=ids[label];result=peer.call('GET',f'/fixture/proof/{job}')
                assert result['snapshot']==before[label]['snapshot']
                view=peer.call('GET',f'/jobs/{job}')
                if reason is None:
                    assert result['events']==[] and view['recovery'] is None
                else:
                    assert len(result['events'])==1 and result['events'][0]['outbox']
                    decision=result['events'][0]['payload']
                    assert decision['reason']==reason and decision['snapshot_sha256']==result['snapshot']
                    assert not decision['replayed'] and not decision['stop_evidence_created']
                    assert view['recovery']==result['recovery'] and not view['recovery']['automatic_replay']
                    assert 'claim_token' not in json.dumps(result) and 'execution_spec' not in json.dumps(result)
                    peer.call('POST',f'/fixture/reconcile-one/{job}')
                    assert peer.call('GET',f'/fixture/proof/{job}')==result
                receipts.append({'label':label,'proof':result,'public':view})
                passed(label+'_durable_decision_without_mutating_execution')
            job=ids['finalizing_dead']
            peer.call('POST',f'/fixture/change/{job}',{'mode':'lease_future'})
            assert peer.call('GET',f'/jobs/{job}')['recovery'] is None
            assert peer.call('POST',f'/fixture/reconcile-one/{job}')['result']=='publication_recovery_required'
            assert len(peer.call('GET',f'/fixture/proof/{job}')['events'])==2
            passed('future_lease_is_not_live_owner_and_old_decision_is_hidden')
            peer.call('POST',f'/fixture/change/{job}',{'mode':'output'})
            assert peer.call('GET',f'/jobs/{job}')['recovery'] is None
            passed('changed_output_invalidates_old_decision')
            concurrent=seed('foreign_host')
            with ThreadPoolExecutor(max_workers=2) as pool:
                values=[future.result(timeout=30) for future in [pool.submit(p.call,'POST',f'/fixture/reconcile-one/{concurrent}') for p in (owner,peer)]]
            assert [v['result'] for v in values]==['owner_unverified']*2
            assert len(peer.call('GET',f'/fixture/proof/{concurrent}')['events'])==1
            passed('two_instances_commit_one_recovery_decision')
            transient=seed('claimed_live_expired')
            original=owner.call('GET',f'/fixture/proof/{transient}')
            marker=owner.root/f'unavailable-{transient}';marker.touch()
            assert owner.call('POST',f'/fixture/reconcile-one/{transient}')['result']=='owner_unverified'
            assert owner.call('GET',f'/jobs/{transient}')['recovery']['reason']=='owner_unverified'
            marker.unlink()
            assert owner.call('POST',f'/fixture/reconcile-one/{transient}')['result']=='owner_live'
            cleared=owner.call('GET',f'/fixture/proof/{transient}')
            assert cleared['snapshot']==original['snapshot'] and cleared['recovery'] is None
            assert len(cleared['events'])==2 and cleared['events'][-1]['type']=='job.recovery_cleared'
            owner.call('POST',f'/fixture/reconcile-one/{transient}')
            assert owner.call('GET',f'/fixture/proof/{transient}')==cleared
            passed('transient_probe_failure_clears_after_live_birth_is_verified_without_execution_change')
            broken=seed('unclaimed')
            owner.call('POST',f'/fixture/change/{broken}',{'mode':'lineage'})
            assert owner.call('POST',f'/fixture/reconcile-one/{broken}')['result']=='lineage_unverified'
            assert owner.call('GET',f'/jobs/{broken}')['recovery']['next_step']=='manual_review'
            passed('inactive_lineage_requires_review_without_queueing')
            for mode,label in [('heartbeat','claimed_live_expired'),('cancel','claimed_dead')]:
                held=seed(label);(owner.root/f'hold-{held}').touch()
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future=pool.submit(owner.call,'POST',f'/fixture/reconcile-one/{held}')
                        wait_file(owner.root/f'observed-{held}')
                        at=time.monotonic()
                        if mode=='cancel':peer.call('POST',f'/jobs/{held}:cancel')
                        else:peer.call('POST',f'/fixture/change/{held}',{'mode':'heartbeat'})
                        elapsed=time.monotonic()-at
                        assert elapsed<8,elapsed
                        (owner.root/f'continue-{held}').touch()
                        assert future.result(timeout=25)['result']=='recovery_changed'
                finally:(owner.root/f'continue-{held}').touch()
                assert peer.call('GET',f'/fixture/proof/{held}')['events']==[]
                view=peer.call('GET',f'/jobs/{held}')
                assert view['status']==('cancelled' if mode=='cancel' else 'queued')
                passed(mode+'_commits_during_probe_and_prevents_stale_recovery')
            peer.create_case('Foreign decision reader')
            for job in ids.values():peer.call('GET',f'/jobs/{job}',expect=404)
            passed('foreign_user_cannot_read_decision')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(owner.root),
        'receipts':receipts,'boundary_phases':'metadata fixtures; OS birth and exit are real',
        'developer_database_touched':False}
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='reconciliation-notice-evidence-',dir=SERVER.parents[1]/'tmp'))
    hashes={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'source.json').write_text(json.dumps(hashes,indent=2),encoding='utf8')
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_notice_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_notice_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    assert hashes=={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('RECONCILIATION_NOTICE_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
