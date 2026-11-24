"""Real trash moves stay charged across scan/move races and OS-lock lifetimes."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import time
import uuid
from testsupport import IsolatedServer,SERVER

FIXTURE='''
import asyncio,time
from pathlib import Path
from fastapi import Depends
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.config import get_settings
from app.services import quota,storage_usage,trash
root=Path(__file__).parent
async def quotas(db,user):return {**quota.DEFAULT_QUOTAS,'storage_mb':int((root/'limit').read_text())}
quota.quotas_for=quotas
scan=storage_usage._scan_fs_tree
def scanning(settings,parts,counter,**kwargs):
 result=scan(settings,parts,counter,**kwargs)
 if parts[0]=='workspaces' and (root/'mode').read_text()=='hold_scan':
  (root/'scan_ready').write_text('ready')
  deadline=time.monotonic()+20
  while not (root/'scan_release').exists():
   if time.monotonic()>deadline:raise TimeoutError('test scan release missing')
   time.sleep(.02)
 return result
storage_usage._scan_fs_tree=scanning
@app.get('/api/v1/fixture/usage')
async def usage(db=Depends(get_db),user=Depends(get_current_user)):return await quota.usage(db,user)
@app.post('/api/v1/fixture/check')
async def check(db=Depends(get_db),user=Depends(get_current_user)):
 await quota.check_storage_quota(db,user,0)
 return {'allowed':True}
@app.post('/api/v1/fixture/timeout')
async def timeout(body:dict,user=Depends(get_current_user)):
 get_settings().storage_measurement_timeout_seconds=body['seconds']
 return {'ok':True}
def hold_lock():
 with trash._mutation_lock():
  (root/'lock_ready').write_text('ready')
  deadline=time.monotonic()+20
  while not (root/'lock_release').exists():
   if time.monotonic()>deadline:raise TimeoutError('test lock release missing')
   time.sleep(.02)
@app.post('/api/v1/fixture/hold-lock')
async def holding(user=Depends(get_current_user)):
 await asyncio.to_thread(hold_lock)
 return {'ok':True}
@app.post('/api/v1/fixture/cancel-scan')
async def cancel_scan(user=Depends(get_current_user)):
 task=asyncio.create_task(storage_usage.measure_user(user.id))
 while not (root/'scan_ready').exists():await asyncio.sleep(.01)
 task.cancel();(root/'scan_cancelled').write_text('cancelled')
 try:await task
 except asyncio.CancelledError:return {'cancelled':True}
 raise AssertionError('Cancellation swallowed')
'''


def boot(url=None,peer=None):
    api=IsolatedServer('trash-storage',app='trash_storage_fixture:app',database_url=url)
    settings={} if peer is None else {key:peer.env[key] for key in
        ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET','VIBEFLOW_INDEX_ROOT')}
    (api.root/'trash_storage_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal');(api.root/'limit').write_text('8')
    if peer:api.token=peer.token
    return api


def usage(api):return api.call('GET','/fixture/usage')
def wait_file(path):
    deadline=time.monotonic()+12
    while not path.exists():
        assert time.monotonic()<deadline,str(path)
        time.sleep(.02)
def release_mode(api):
    (api.root/'scan_release').write_text('release');(api.root/'mode').write_text('normal')
def arm(api):
    for name in ('scan_release','scan_ready','scan_cancelled'):(api.root/name).unlink(missing_ok=True)
    (api.root/'mode').write_text('hold_scan')


def verify(url=None):
    checks=[];api=boot(url)
    with api:
        case=api.create_case('Retained trash storage');prefix=api.prefix
        api.put('Ref/retained.bin','x'*(2*1024*1024))
        workspace=api.call('GET','/workspaces')[0]
        second=api.call('POST',f"/workspaces/{workspace['id']}/cases",{'name':'Second retained case'})
        upload=api.call('POST',prefix+'/uploads',{'path':'Ref/pending.bin','size':65536})
        initial=usage(api)
        with boot(url,api) as peer:
            api.call('DELETE',prefix)
            deleted=usage(peer)
            assert deleted==initial,(deleted,initial)
            assert list((api.root/'files/_trash/users').rglob('retained.bin'))[0].stat().st_size==2*1024*1024
            (peer.root/'limit').write_text('1');peer.call('POST','/fixture/check',expect=429);(peer.root/'limit').write_text('8')
            checks.append('real_case_trash_keeps_payload_and_active_upload_charged')
            peer.call('POST',prefix+':restore')
            assert usage(peer)==initial
            peer.call('DELETE',prefix+'/uploads/'+upload['id'])
            assert usage(peer)['storage_bytes_reserved']==0
            checks.append('restore_preserves_inventory_and_upload_can_be_cancelled_after_restore')

            baseline=usage(peer)
            api.call('DELETE',f"/workspaces/{workspace['id']}")
            assert usage(peer)==baseline
            peer.call('POST',f"/workspaces/{workspace['id']}:restore")
            assert usage(peer)==baseline
            checks.append('whole_workspace_trash_and_restore_preserve_all_case_bytes')

            uid=workspace['user_id'] if 'user_id' in workspace else next((api.root/'files/workspaces').iterdir()).name
            trash_root=api.root/f'files/_trash/users/{uid}'
            orphan=trash_root/'cases/unindexed/retained.bin';orphan.parent.mkdir(parents=True,exist_ok=True);orphan.write_bytes(b'o'*65536)
            neighbor=api.root/f'files/_trash/users/{uid}0/cases/neighbor/foreign.bin';neighbor.parent.mkdir(parents=True);neighbor.write_bytes(b'n'*131072)
            assert usage(peer)['storage_bytes_used']==baseline['storage_bytes_used']+65536
            checks.append('physical_orphan_trash_counted_without_index_and_neighbor_is_excluded')

            import subprocess
            link=trash_root/'foreign-junction'
            created=subprocess.run(['cmd','/c','mklink','/J',str(link),str(neighbor.parent)],capture_output=True)
            assert created.returncode==0
            try:peer.call('GET','/fixture/usage',expect=503)
            finally:link.rmdir()
            assert neighbor.read_bytes()==b'n'*131072
            checks.append('trash_junction_is_refused_without_reading_or_deleting_foreign_tree')

            for operation in ('restore','delete'):
                if operation=='restore':api.call('DELETE',prefix)
                expected=usage(peer)
                arm(api)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    measured=pool.submit(usage,api);wait_file(api.root/'scan_ready')
                    moved=pool.submit(peer.call,'POST' if operation=='restore' else 'DELETE',prefix+(':restore' if operation=='restore' else ''))
                    try:
                        time.sleep(.2);assert not moved.done()
                        assert peer.call('GET','/api/health',api_prefix=False)['ok']
                    finally:release_mode(api)
                    assert measured.result(timeout=25)==expected
                    moved.result(timeout=25)
                assert usage(peer)==expected
                checks.append('cross_API_'+operation+'_cannot_cross_the_two_scan_roots')

            expected=usage(peer);arm(api)
            with ThreadPoolExecutor(max_workers=2) as pool:
                cancelled=pool.submit(api.call,'POST','/fixture/cancel-scan')
                wait_file(api.root/'scan_cancelled')
                restored=pool.submit(peer.call,'POST',prefix+':restore')
                try:
                    time.sleep(.2);assert not cancelled.done() and not restored.done()
                finally:release_mode(api)
                assert cancelled.result(timeout=25)=={'cancelled':True};restored.result(timeout=25)
            assert usage(peer)==expected
            checks.append('cancelled_scan_keeps_move_lock_until_actual_thread_finishes')

            for holder in (api,peer):
                for name in ('lock_ready','lock_release'):(holder.root/name).unlink(missing_ok=True)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    held=pool.submit(holder.call,'POST','/fixture/hold-lock');wait_file(holder.root/'lock_ready')
                    api.call('POST','/fixture/timeout',{'seconds':.1})
                    try:
                        start=time.monotonic();api.call('GET','/fixture/usage',expect=503)
                        assert time.monotonic()-start<2
                    finally:
                        (holder.root/'lock_release').write_text('release')
                        api.call('POST','/fixture/timeout',{'seconds':30})
                    held.result(timeout=25)
                assert usage(api)==expected
                checks.append(('thread' if holder is api else 'process')+'_lock_wait_obeys_inventory_budget_and_recovers')

            for name in ('lock_ready','lock_release'):(peer.root/name).unlink(missing_ok=True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                held=pool.submit(peer.call,'POST','/fixture/hold-lock');wait_file(peer.root/'lock_ready')
                peer.proc.terminate();peer.proc.wait(timeout=10)
                try:held.result(timeout=10)
                except OSError:pass
                else:raise AssertionError('Killed lock holder returned success')
            assert usage(api)==expected
            checks.append('real_process_exit_releases_move_lock_without_zeroing_retained_bytes')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'trash-storage-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('PASS '+json.dumps(result),flush=True);return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='trash-storage-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_trashstorage_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_trashstorage_[0-9a-f]{32}',name)
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
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('TRASH_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
