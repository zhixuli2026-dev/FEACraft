"""Actual shared legacy upload owner reconciliation, SQL quota and two APIs."""
import argparse,asyncio,hashlib,json,re,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER

SOURCES=['app/services/upload_storage.py','app/routers/files.py','app/services/uploads.py','app/services/owner_storage_budget.py']
def hashes():return {n:hashlib.sha256((SERVER/n).read_bytes()).hexdigest() for n in SOURCES}
FIXTURE=r'''
import asyncio,json,os
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app import storage
from app.db import get_db,SessionLocal
from app.deps import get_current_user
from app.models import User,Case,Workspace,Membership,Plan,Subscription,UploadStorageReservation,DurableEvent
from app.routers.workspaces import get_owned_case
from app.services import quota,upload_storage,owner_storage_budget as capacity,uploads
from app.services.durable_events import json_text
tickets={};held=None;locks_ready=asyncio.Event();locks_release=asyncio.Event()
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import upload_owner_reconciliation as owner_recovery
original_append=owner_recovery.append_event;original_commit=AsyncSession.commit
crash_selected=None;crash_mode=None;commit_entered=asyncio.Event();commit_release=asyncio.Event();migration_tasks={}
async def appending(db,case_id,event_type,payload,**kwargs):
 value=await original_append(db,case_id,event_type,payload,**kwargs)
 db.info['owner_reconciliation_upload']=payload['upload_id'];migration_tasks[payload['upload_id']]=asyncio.current_task()
 return value
owner_recovery.append_event=appending
async def committing(db):
 selected=db.info.get('owner_reconciliation_upload')
 active=selected is not None and selected==crash_selected
 if active and crash_mode=='exit_before_commit':os._exit(79)
 if active and crash_mode=='pause_commit':commit_entered.set();await commit_release.wait()
 value=await original_commit(db)
 if active and crash_mode=='exit_after_commit':os._exit(79)
 return value
AsyncSession.commit=committing
@app.post('/api/v1/testing/owner-commit-control')
async def commit_control(body:dict,user=Depends(get_current_user)):
 global crash_selected,crash_mode
 action=body['action']
 if action=='release':commit_release.set()
 elif action=='cancel':
  task=migration_tasks[body['upload_id']];assert not task.done();task.cancel()
 else:
  assert action in {'exit_before_commit','exit_after_commit','pause_commit'}
  crash_selected=body['upload_id'];crash_mode=action;commit_entered.clear();commit_release.clear()
 return {'entered':commit_entered.is_set()}
@app.get('/api/v1/testing/owner-commit-control')
async def commit_state(user=Depends(get_current_user)):return {'entered':commit_entered.is_set()}
@app.get('/api/v1/testing/owner-pid')
async def pid(user=Depends(get_current_user)):return {'pid':os.getpid()}
@app.post('/api/v1/testing/owner-share/{case_id}/{member_id}')
async def share(case_id:int,member_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db);ws=await db.get(Workspace,case.workspace_id);assert ws.user_id==user.id
 member=await db.get(User,member_id);assert member is not None
 personal=await quota.ensure_user_org(db,member)
 plan=Plan(code='test-shared-'+str(member_id),name='Actual isolated collaborator plan',quotas=json_text({**quota.DEFAULT_QUOTAS,'storage_mb':64}))
 db.add(plan);await db.flush()
 sub=await db.scalar(select(Subscription).where(Subscription.org_id==personal.id));assert sub is not None;sub.plan_id=plan.id
 org=await quota.ensure_user_org(db,user)
 db.add(Membership(org_id=org.id,user_id=member_id,role='member'));await db.commit()
 return {'workspace_id':ws.id,'org_id':org.id}
@app.post('/api/v1/testing/owner-seed/{case_id}/{upload_id}')
async def seed(case_id:int,upload_id:str,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db);ws=await db.get(Workspace,case.workspace_id);assert ws.user_id==user.id
 row=await db.get(UploadStorageReservation,upload_id);assert row.case_id==case.id
 if 'user_id' in body:row.user_id=body['user_id']
 if 'binding' in body:row.binding_json=body['binding']
 await db.commit();return {'seeded_actual_SQL_row':True}
@app.post('/api/v1/testing/owner-limit')
async def limit(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 plan=await db.scalar(select(Plan).where(Plan.code=='free'));q=json.loads(plan.quotas);q['storage_mb']=body['storage_mb'];plan.quotas=json_text(q);await db.commit();return {'changed':True}
@app.get('/api/v1/testing/owner-ledger/{case_id}/{upload_id}')
async def ledger(case_id:int,upload_id:str,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db);ws=await db.get(Workspace,case.workspace_id);assert ws.user_id==user.id
 row=await db.get(UploadStorageReservation,upload_id,populate_existing=True)
 events=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case.id,DurableEvent.type=='files.upload_owner_reconciled')))
 return {'id':row.id,'user_id':row.user_id,'remaining':row.remaining_bytes,'state':row.state,'binding_json':row.binding_json,
 'events':[json.loads(e.payload_json) for e in events if json.loads(e.payload_json)['upload_id']==upload_id]}
@app.post('/api/v1/testing/owner-pool')
async def pool(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 if body['action']=='acquire':
  ticket=await capacity.acquire(user.id);tickets[ticket['id']]=ticket;return {'id':ticket['id']}
 ticket=tickets.pop(body['id'])
 assert ticket['user_id']==user.id
 async with SessionLocal() as actual:await capacity.finish(capacity.Scope(actual,ticket))
 return {'settled_actual_unused_pool':True}
@app.post('/api/v1/testing/owner-lock')
async def owner_lock(body:dict,user=Depends(get_current_user)):
 global held
 from app.services.storage_admission import lock_owner
 if body['action']=='acquire':
  assert held is None;locks_ready.clear();locks_release.clear()
  async def hold():
   async with SessionLocal() as db:
    await lock_owner(db,user.id);locks_ready.set();await locks_release.wait();await db.rollback()
  held=asyncio.create_task(hold());await locks_ready.wait();return {'actual_SQL_lock_held':True}
 locks_release.set();await held;held=None;return {'actual_SQL_lock_released':True}
'''


def verify(url=None,store=None,before_fix=False):
    from app.services import process_identity_native as identity
    api=IsolatedServer('upload-owner-api',app='upload_owner_fixture:app',database_url=url)
    peer=IsolatedServer('upload-owner-peer',app='upload_owner_peer:app',database_url=url)
    settings=store.settings() if store else {}
    (api.root/'upload_owner_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    report={'passed':False,'before_fix':before_fix,'storage':'s3' if store else 'fs','database':'postgresql' if url else 'sqlite','model_calls':0,'mock_llm_calls':0,'checks':[]};checks=report['checks'];births={}
    print('UPLOAD_OWNER_API '+str(api.root),flush=True)
    def request(server,method,path,token=None,**kwargs):return httpx.request(method,server.base+'/api/v1'+path,headers={'Authorization':'Bearer '+(token or server.token)},trust_env=False,timeout=30,**kwargs)
    def expect(response,status=200):assert response.status_code==status,(response.status_code,response.text[:1400]);return response.json()
    def ledger(identifier,server=None):return (server or api).call('GET','/testing/owner-ledger/'+str(api.case_id)+'/'+identifier)
    def seed(identifier,body):return api.call('POST','/testing/owner-seed/'+str(api.case_id)+'/'+identifier,body)
    def make(name,payload):return api.call('POST',api.prefix+'/uploads',{'path':'Ref/'+name,'size':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
    try:
        with api:
            api.create_case('Actual legacy shared upload owner')
            births['main']=identity.observe(api.call('GET','/testing/owner-pid')['pid'])
            owner_id=api.call('GET','/auth/me')['id']
            other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Actual shared collaborator'})
            member=expect(request(api,'GET','/auth/me',token=other['token']))['id']
            shared=api.call('POST',f'/testing/owner-share/{api.case_id}/{member}')
            api.call('PUT',f'/workspaces/{shared["workspace_id"]}/sharing',{'org_id':shared['org_id'],'role':'member','enabled':True})
            api.call('POST','/testing/owner-limit',{'storage_mb':64})
            left=b'Actual legacy first half'*1024;right=b'Actual continuing half'*1024;payload=left+right
            upload=make('legacy.bin',payload);target=api.prefix+'/uploads/'+upload['id']
            expect(request(api,'PUT',target+'?offset=0',content=left))
            seed(upload['id'],{'user_id':member});old=ledger(upload['id']);assert old['remaining']==len(right)
            if before_fix:
                listed=api.call('GET',api.prefix+'/uploads');assert upload['id'] not in {r['id'] for r in listed}
                denied=expect(request(api,'PUT',target+'?offset='+str(len(left)),content=right),404)
                assert ledger(upload['id'])==old
                report['expected_product_counterexample_confirmed']=True
                checks.append({'check':'actual_valid_shared_legacy_upload_hidden_and_404_on_owner_resume','response':denied})
                report['functional_passed']=True
            else:
                shared_env={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')};shared_env.update(settings)
                (peer.root/'upload_owner_peer.py').write_text('import os\nos.environ.update('+repr(shared_env)+')\n'+FIXTURE,encoding='utf8');peer.token=api.token
                with peer:
                    births['peer']=identity.observe(peer.call('GET','/testing/owner-pid')['pid'])
                    listed=api.call('GET',api.prefix+'/uploads');entry=next(r for r in listed if r['id']==upload['id']);assert entry['owner_reconciliation_required'] and entry['can_reconcile_owner']
                    denied=expect(request(api,'PUT',target+'?offset='+str(len(left)),content=right),409)
                    assert denied['detail']['code']=='upload_owner_reconciliation_required'
                    expect(request(api,'POST',target+':reconcile-owner',token=other['token'],json={}),404)
                    assert ledger(upload['id'])==old
                    checks.append({'check':'legacy_visible_explicit_recovery_error_and_only_owner_authorized'})
                    api.call('POST','/testing/owner-limit',{'storage_mb':0})
                    api.call('POST',target+':reconcile-owner',{},expect=429);assert ledger(upload['id'])==old
                    api.call('POST','/testing/owner-limit',{'storage_mb':64})
                    checks.append({'check':'new_owner_quota_refusal_retains_old_claim_and_no_event'})
                    ticket=expect(request(api,'POST','/testing/owner-pool',token=other['token'],json={'action':'acquire'}))
                    blocked=expect(request(api,'POST',target+':reconcile-owner',json={}),409)
                    assert blocked['detail']['code']=='upload_owner_recovery_required' and ledger(upload['id'])==old
                    expect(request(api,'POST','/testing/owner-pool',token=other['token'],json={'action':'finish','id':ticket['id']}))
                    checks.append({'check':'actual_old_owner_pool_blocks_transfer_until_creator_settles'})
                    # Hold the old owner's real SQL lock in an independent API.
                    expect(request(peer,'POST','/testing/owner-lock',token=other['token'],json={'action':'acquire'}))
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future=pool.submit(request,api,'POST',target+':reconcile-owner',json={})
                        time.sleep(.1)
                        if future.done():assert future.result().status_code==409
                        expect(request(peer,'POST','/testing/owner-lock',token=other['token'],json={'action':'release'}))
                        response=future.result();assert response.status_code in {200,409}
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        responses=[f.result() for f in [pool.submit(request,s,'POST',target+':reconcile-owner',json={}) for s in (api,peer)]]
                    assert any(r.status_code==200 for r in responses) and all(r.status_code in {200,409} for r in responses)
                    replay=api.call('POST',target+':reconcile-owner',{});assert replay['replayed']
                    current=ledger(upload['id']);assert current['user_id']==owner_id and current['remaining']==len(right) and len(current['events'])==1
                    event=current['events'][0];assert event['old_user_id']==member and event['new_user_id']==owner_id and event['old_remaining_bytes']==event['remaining_bytes']==len(right)
                    assert current['binding_json']==old['binding_json']
                    checks.append({'check':'actual_old_owner_lock_and_two_API_transfer_single_audit','statuses':[r.status_code for r in responses]})
                    expect(request(peer,'PUT',target+'?offset='+str(len(left)),content=right,token=other['token']))
                    api.call('POST',target+':complete',{});api.call('POST',target+':complete',{})
                    assert api.call('GET',api.prefix+'/raw?path=Ref/legacy.bin',raw=True)==payload
                    final=ledger(upload['id']);assert final['state']=='completed' and final['remaining']==0 and len(final['events'])==1
                    checks.append({'check':'shared_member_continues_then_owner_completes_exact_original_bytes'})
                    broken=make('binding.bin',b'Known bound data');seed(broken['id'],{'user_id':member});original=ledger(broken['id'])
                    binding=json.loads(original['binding_json']);binding['prefix']+='/changed';seed(broken['id'],{'binding':json.dumps(binding)})
                    api.call('POST',api.prefix+'/uploads/'+broken['id']+':reconcile-owner',{},expect=409)
                    assert not ledger(broken['id'])['events'] and ledger(broken['id'])['user_id']==member
                    seed(broken['id'],{'binding':original['binding_json']});api.call('POST',api.prefix+'/uploads/'+broken['id']+':reconcile-owner',{})
                    api.call('DELETE',api.prefix+'/uploads/'+broken['id'])
                    checks.append({'check':'explicit_mismatched_binding_fault_rejected_then_original_binding_reconciled_and_cancelled'})
                    pending={}
                    for action in ['pause_commit','exit_before_commit','exit_after_commit']:
                        item=make('atomic-'+action+'.bin',b'Actual untouched pending content')
                        seed(item['id'],{'user_id':member});pending[action]=item
                    item=pending['pause_commit'];path=api.prefix+'/uploads/'+item['id']+':reconcile-owner'
                    api.call('POST','/testing/owner-commit-control',{'action':'pause_commit','upload_id':item['id']})
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future=pool.submit(request,api,'POST',path,json={});until=time.monotonic()+10
                        while not api.call('GET','/testing/owner-commit-control')['entered']:
                            assert not future.done() and time.monotonic()<until;time.sleep(.02)
                        for _ in range(3):api.call('POST','/testing/owner-commit-control',{'action':'cancel','upload_id':item['id']})
                        assert not future.done();api.call('POST','/testing/owner-commit-control',{'action':'release'})
                        assert future.result().status_code==500
                    actual=ledger(item['id'],peer);assert actual['user_id']==owner_id and len(actual['events'])==1
                    assert peer.call('POST',path,{})['replayed']
                    peer.call('DELETE',api.prefix+'/uploads/'+item['id'])
                    checks.append({'check':'three_actual_request_cancellations_wait_for_commit_and_audit_together'})
                    item=pending['exit_before_commit'];path=api.prefix+'/uploads/'+item['id']+':reconcile-owner'
                    api.call('POST','/testing/owner-commit-control',{'action':'exit_before_commit','upload_id':item['id']})
                    try:response=request(api,'POST',path,json={})
                    except httpx.TransportError:pass
                    else:raise AssertionError('Abrupt exit before SQL commit returned HTTP '+str(response.status_code))
                    until=time.monotonic()+10
                    while identity.probe(births['main'])['state']!='exited':assert time.monotonic()<until;time.sleep(.02)
                    actual=ledger(item['id'],peer);assert actual['user_id']==member and not actual['events']
                    peer.call('POST',path,{});assert len(ledger(item['id'],peer)['events'])==1
                    peer.call('DELETE',api.prefix+'/uploads/'+item['id'])
                    checks.append({'check':'actual_API_exit_before_commit_rolls_back_owner_and_audit_then_peer_reconciles'})
                    killer=IsolatedServer('upload-owner-commit-exit',app='upload_owner_killer:app',database_url=url)
                    (killer.root/'upload_owner_killer.py').write_text('import os\nos.environ.update('+repr(shared_env)+')\n'+FIXTURE,encoding='utf8');killer.token=api.token
                    with killer:
                        births['commit_exit']=identity.observe(killer.call('GET','/testing/owner-pid')['pid'])
                        item=pending['exit_after_commit'];path=api.prefix+'/uploads/'+item['id']+':reconcile-owner'
                        killer.call('POST','/testing/owner-commit-control',{'action':'exit_after_commit','upload_id':item['id']})
                        try:response=request(killer,'POST',path,json={})
                        except httpx.TransportError:pass
                        else:raise AssertionError('Abrupt exit after SQL commit returned HTTP '+str(response.status_code))
                        until=time.monotonic()+10
                        while identity.probe(births['commit_exit'])['state']!='exited':assert time.monotonic()<until;time.sleep(.02)
                    actual=ledger(item['id'],peer);assert actual['user_id']==owner_id and len(actual['events'])==1
                    assert peer.call('POST',path,{})['replayed'];assert len(ledger(item['id'],peer)['events'])==1
                    peer.call('DELETE',api.prefix+'/uploads/'+item['id'])
                    checks.append({'check':'actual_API_exit_after_commit_peer_replays_single_persisted_owner_audit'})
                    report['functional_passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1800]};raise
    finally:
        report['births']=births;report['exit_probes']={}
        for name,birth in births.items():
            until=time.monotonic()+10
            while True:
                probe=identity.probe(birth)
                if probe['state']=='exited' or time.monotonic()>=until:break
                time.sleep(.03)
            report['exit_probes'][name]=probe
        report['passed']=bool(report.get('functional_passed') and all(v['state']=='exited' for v in report['exit_probes'].values()))
        report['peer_root']=str(peer.root) if 'peer' in births else None;report['source_hashes']=hashes()
        module=SERVER/'app/services/upload_owner_reconciliation.py'
        if module.exists():report['source_hashes']['app/services/upload_owner_reconciliation.py']=hashlib.sha256(module.read_bytes()).hexdigest()
        (api.root/'owner-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    assert report['passed'];print('UPLOAD_OWNER_OK '+str(api.root)+' checks='+str(len(checks)),flush=True);return api.root


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--before-fix',action='store_true');parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');a=parser.parse_args()
    def run(url=None):
        if not a.s3:return verify(url,before_fix=a.before_fix)
        from s3testsupport import ObjectStore
        store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed');result={'passed':False}
        print('UPLOAD_OWNER_S3 '+str(store.root),flush=True)
        try:
            with store:
                root=verify(url,store,before_fix=a.before_fix);result.update(passed=True,api_root=str(root));return root
        finally:
            result['store_stopped']=store.proc is not None and store.proc.poll() is not None
            (store.root/'owner-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    if not a.postgres:await asyncio.to_thread(run);return
    import asyncpg,tempfile
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    u=make_url(Settings(_env_file=SERVER/'.env').database_url);assert u.drivername=='postgresql+asyncpg' and u.host in {'localhost','127.0.0.1','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='upload-owner-postgres-',dir=SERVER.parents[1]/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False}
    print('UPLOAD_OWNER_POSTGRES '+str(root),flush=True)
    conn=await asyncpg.connect(host=u.host,port=u.port or 5432,user=u.username,password=u.password,database='postgres',timeout=10);created=False
    try:
        await conn.execute('CREATE DATABASE "'+name+'"');created=True;url=u.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head');report.update(api_root=str(await asyncio.to_thread(run,url)),passed=True)
    finally:
        try:
            if created:await conn.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await conn.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
if __name__=='__main__':asyncio.run(main())
