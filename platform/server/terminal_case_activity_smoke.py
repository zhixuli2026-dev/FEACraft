"""Real terminal lifetime and durable evidence fence case administration on both APIs."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

import websockets
from sqlalchemy.engine import make_url

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from job_release_smoke import wait_file
from app.services import process_identity_native as identity

CONTROLLED = '''import asyncio,json,time,uuid
from datetime import timedelta
from pathlib import Path
from fastapi import Depends,HTTPException
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import NativeSession,Case
from app import storage
from app.services import terminal_records as records
from app.services.durable_events import now
root=Path(__file__).parent
async def pause(name):
 (root/(name+'.ready')).touch()
 deadline=time.monotonic()+45
 while not (root/(name+'.release')).exists() and time.monotonic()<deadline:await asyncio.sleep(.02)
 if not (root/(name+'.release')).exists():raise TimeoutError('Terminal activity barrier')
create=records.create
async def creating(case_id,*args,**kwargs):
 if (root/'before-create.armed').exists():await pause('before-create')
 owner=await create(case_id,*args,**kwargs)
 if (root/'starting.armed').exists():
  (root/'starting.json').write_text(json.dumps({'id':owner.id,'case_id':case_id}))
  await pause('starting')
 return owner
records.create=creating
complete=records.complete
async def completing(owner,process,stop,**kwargs):
 if (root/'closing.armed').exists():
  assert stop['verified'] is True
  (root/'closing.json').write_text(json.dumps({'id':owner.id,'root':process,'stop':stop}))
  await pause('closing')
 return await complete(owner,process,stop,**kwargs)
records.complete=completing
from app.main import app
saved={};residues={}
@app.post('/api/v1/fixture/terminal/{sid}/evidence')
async def evidence(sid:str,body:dict,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  row=await db.get(NativeSession,sid)
  if row is None or row.created_by!=user.id:raise HTTPException(404)
  fields=('phase','error','process_json','stop_json','lease_expires_at','profile_json','ended_at')
  action=body['action']
  if action=='restore':
   for key,value in saved.pop(sid).items():setattr(row,key,value)
   target=residues.pop(sid,None)
   if target is not None:
    base=storage._fs_root().resolve();assert target.resolve()!=base and target.resolve().is_relative_to(base)
    if target.is_dir():target.rmdir()
    else:target.unlink()
  else:
   assert sid not in saved and row.phase=='closed'
   saved[sid]={key:getattr(row,key) for key in fields}
   if action=='expired':row.phase='active';row.stop_json='{}';row.ended_at=None;row.lease_expires_at=now()-timedelta(seconds=60)
   elif action=='invalid':row.process_json='invalid JSON'
   elif action in {'missing_stop','unregistered','failed_verified'}:
    row.phase='failed';row.error='InjectedEvidenceError'
    if action!='failed_verified':row.stop_json='{}'
    if action=='unregistered':row.process_json='{}'
   elif action.startswith('staging_'):
    selected=json.loads(row.profile_json)
    stage='.staging/job-activity-'+uuid.uuid4().hex
    selected.update(source_prefix=(await db.get(Case,row.case_id)).s3_prefix,
      staging_prefix='invalid-stage' if action=='staging_invalid' else stage)
    row.profile_json=json.dumps(selected)
    if action in {'staging_directory','staging_manifest','staging_cleanup'}:
     target=(storage._fs_path(stage,'.') if action=='staging_directory' else
       storage._staging_manifest_path(stage) if action=='staging_manifest' else storage._terminal_cleanup_marker(stage))
     target.parent.mkdir(parents=True,exist_ok=True)
     if action=='staging_directory':target.mkdir(exist_ok=False)
     else:
      with target.open('x',encoding='utf8') as handle:handle.write('{}')
     residues[sid]=target
   else:raise ValueError(action)
  await db.commit()
  return records.public(row)
'''


async def connect(api, case_id=None, *, rejected=False):
    url=api.base.replace('http','ws',1)+f'/ws/terminal?case={case_id or api.case_id}&token={api.token}'
    async with websockets.connect(url) as socket:
        if rejected:
            frames=[]
            try:
                while True:
                    frame=await asyncio.wait_for(socket.recv(),10)
                    if isinstance(frame,str):frames.append(json.loads(frame))
            except websockets.ConnectionClosed:
                assert socket.close_code==4403 and not any(v.get('type')=='ready' for v in frames),(socket.close_code,frames)
            return frames
        ready=json.loads(await socket.recv())
        assert ready['type']=='ready' and ready['alive'],ready
        return ready


async def verify(database_url=None):
    api=IsolatedServer('terminal-case-activity',app='activity_fixture:app',database_url=database_url)
    (api.root/'activity_fixture.py').write_text(CONTROLLED,encoding='utf8')
    checks=[];receipts=[]
    with api:
        api.create_case('Terminal activity')
        case=api.call('GET',api.prefix);ws=case['workspace_id']
        peer=IsolatedServer('terminal-case-activity-peer',app='peer_boot:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'peer_boot.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf8')
        with peer:
            peer.token=api.token
            destination=peer.call('POST','/workspaces',{'name':'Completed activity copies'})['id']

            def blocked(label):
                replies=[]
                for server in (api,peer):
                    for method,path,body in (
                        ('POST',api.prefix+':copy',{'name':'Blocked copy'}),
                        ('POST',f'/workspaces/{ws}:copy',{'name':'Blocked workspace'}),
                        ('DELETE',api.prefix,None),('DELETE',f'/workspaces/{ws}',None)):
                        start=time.monotonic();reply=server.call(method,path,body,expect=409)
                        elapsed=time.monotonic()-start
                        assert '终端' in reply['detail'] and elapsed<3,(label,method,path,reply,elapsed)
                        replies.append({'path':path,'peer':server is peer,'seconds':elapsed,'reply':reply})
                assert peer.call('GET',api.prefix)['id']==api.case_id
                receipts.append({'label':label,'replies':replies});checks.append(label)

            def copy_allowed(label):
                copied=peer.call('POST',api.prefix+':copy',{'name':label,'workspace_id':destination})
                assert copied['id']!=api.case_id
                peer.call('DELETE',f"/cases/{copied['id']}")
                checks.append(label)

            sid=None
            try:
                (api.root/'starting.armed').touch()
                connecting=asyncio.create_task(connect(api))
                try:
                    await asyncio.to_thread(wait_file,api.root/'starting.ready',seconds=20)
                    sid=json.loads((api.root/'starting.json').read_text())['id']
                    state=peer.call('GET',f'/terminal/sessions/{sid}')
                    assert state['phase']=='starting' and state['process']['root'] is None,state
                    blocked('starting_blocks_both_APIs_without_a_native_root')
                finally:
                    (api.root/'starting.release').touch();(api.root/'starting.armed').unlink(missing_ok=True)
                    ready=await asyncio.wait_for(connecting,30)
                    sid=ready['session']
                state=peer.call('GET',f'/terminal/sessions/{sid}')
                root=state['process']['root'];gate=state['process']['gate']
                assert identity.probe(root['host'])['state']=='match'
                assert identity.probe(gate['process'])['state']=='match'
                blocked('active_native_terminal_blocks_copy_and_trash_across_APIs')
                other=peer.call('POST',f'/workspaces/{ws}/cases',{'name':'Unrelated case'})
                copied=peer.call('POST',f"/cases/{other['id']}:copy",{'name':'Unrelated copy'})
                peer.call('DELETE',f"/cases/{copied['id']}");peer.call('DELETE',f"/cases/{other['id']}")
                peer.call('POST',f"/cases/{copied['id']}:restore");peer.call('POST',f"/cases/{other['id']}:restore")
                checks.append('unrelated_case_remains_available')
                (api.root/'closing.armed').touch()
                closing=asyncio.create_task(asyncio.to_thread(api.call,'DELETE',f'/terminal/sessions/{sid}'))
                try:
                    await asyncio.to_thread(wait_file,api.root/'closing.ready',seconds=20)
                    assert identity.probe(root['host'])['state']=='exited'
                    blocked('actual_exit_without_committed_stop_still_blocks_administration')
                finally:
                    (api.root/'closing.release').touch();(api.root/'closing.armed').unlink(missing_ok=True)
                    await asyncio.wait_for(closing,30)
                state=peer.call('GET',f'/terminal/sessions/{sid}')
                assert state['phase']=='closed' and state['cleanup_verified'] is True,state
                copy_allowed('verified_close_allows_copy')
                for action,phase in [('expired','recovery_required'),('invalid','evidence_invalid'),
                                     ('missing_stop','failed'),('unregistered','failed'),
                                     ('staging_directory','closed'),('staging_manifest','closed'),
                                     ('staging_cleanup','closed'),('staging_invalid','closed')]:
                    state=api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':action})
                    try:
                        assert state['phase']==phase,state
                        blocked(action+'_evidence_does_not_authorize_filesystem_changes')
                    finally:api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':'restore'})
                api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':'failed_verified'})
                try:copy_allowed('failed_session_with_verified_stop_allows_copy')
                finally:api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':'restore'})
                api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':'staging_cleared'})
                try:copy_allowed('verified_close_without_staging_residue_allows_copy')
                finally:api.call('POST',f'/fixture/terminal/{sid}/evidence',{'action':'restore'})
                copied=peer.call('POST',f'/workspaces/{ws}:copy',{'name':'Closed terminal workspace'})
                peer.call('DELETE',f"/workspaces/{copied['id']}")
                peer.call('DELETE',api.prefix);peer.call('POST',api.prefix+':restore')
                peer.call('DELETE',f'/workspaces/{ws}');peer.call('POST',f'/workspaces/{ws}:restore')
                checks.append('verified_close_allows_workspace_copy_case_and_workspace_trash_restore')
                stale=peer.call('POST',f'/workspaces/{ws}/cases',{'name':'Delete before terminal reservation'})
                (api.root/'before-create.armed').touch()
                pending=asyncio.create_task(connect(api,stale['id'],rejected=True))
                try:
                    await asyncio.to_thread(wait_file,api.root/'before-create.ready',seconds=20)
                    peer.call('DELETE',f"/cases/{stale['id']}")
                finally:
                    (api.root/'before-create.release').touch();(api.root/'before-create.armed').unlink(missing_ok=True)
                    await asyncio.wait_for(pending,30)
                assert not any(row['case_id']==stale['id'] for row in peer.call('GET','/terminal/sessions?include_closed=true')['sessions'])
                peer.call('POST',f"/cases/{stale['id']}:restore")
                checks.append('case_delete_after_selection_rejects_reservation_before_row_or_native_launch')
            finally:
                for name in ('starting','closing','before-create'):(api.root/(name+'.release')).touch()
                if sid is not None:api.call('DELETE',f'/terminal/sessions/{sid}')
    result={'passed':True,'checks':checks,'count':len(checks),'receipts':receipts,'evidence':str(api.root),
            'peer_evidence':str(peer.root),'actual_native_terminal':True,'developer_database_touched':False}
    (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='terminal-case-activity-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await verify()}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_termactivity_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_termactivity_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await verify(temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('TERMINAL_CASE_ACTIVITY_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
