"""Real VTK with file-backed controlled S3, cleanup, conflicts and cancellation."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from terminal_staging_smoke import CONTROLLED as TERMINAL_FIXTURE
from viz_execution_smoke import STL,request,until

# Share the storage fault harness, while keeping production process code and
# native visualization records intact. This is a controlled facade, not S3.
CONTROLLED=TERMINAL_FIXTURE.replace('NativeSession','NativeVizSession').replace('terminal_records','viz_records')
CONTROLLED=CONTROLLED.replace('from app.routers import terminal','from app.routers import terminal,viz')
CONTROLLED=CONTROLLED.replace('new=terminal._new_session','new=viz._create').replace('terminal._new_session=starting','viz._create=starting')
CONTROLLED=CONTROLLED.replace('identifier:str','identifier:int')
CONTROLLED+='\nviz.prepare_job_staging=preparing\nviz._use_s3=storage._use_s3\n'


async def verify(database_url=None,selected=None,boundary=None):
    from staging_liveness_test import instrument,verify_liveness
    server=IsolatedServer('viz-staging',app='controlled_viz_staging:app',database_url=database_url)
    (server.root/'controlled_viz_staging.py').write_text(instrument(CONTROLLED,boundary),encoding='utf8');checks=[];runs=[]
    with server as api:
        api.create_case('viz staging');workspace=api.call('GET','/workspaces')[0]
        await request(api,'POST','/fixture/staging',api=False)
        for mode in ('success','conflict','case_unavailable','source_rebound','sync_hold','cleanup_retry','cleanup_failure'):
            if selected and mode not in selected:continue
            case=await request(api,'POST',f"/workspaces/{workspace['id']}/cases",{'name':'viz '+mode});case_id=case['id']
            await request(api,'PUT',f'/cases/{case_id}/file',{'path':'D-post/cube.stl','content':STL})
            await request(api,'PUT',f'/cases/{case_id}/file',{'path':'Ref/viz-data.txt','content':'BASE'})
            await request(api,'POST','/fixture/mode/'+mode,api=False)
            session=await request(api,'POST',f'/cases/{case_id}/viz:sessions',{'file_path':'D-post/cube.stl'});sid=session['id']
            snapshot=await request(api,'GET',f'/fixture/session/{sid}',api=False)
            stage=Path(snapshot['stage']);remote=Path(snapshot['remote']);manifest=Path(snapshot['manifest']);marker=Path(snapshot['cleanup_marker'])
            assert stage.is_relative_to(api.root/'files') and stage.exists() and manifest.exists()
            assert (stage/f".vibeflow/viz/viz-{session['port']}.log").exists()
            (stage/'Ref/viz-data.txt').write_text('STAGED',encoding='utf8')
            assert (remote/'Ref/viz-data.txt').read_text()=='BASE'
            if mode=='conflict':await request(api,'PUT',f'/cases/{case_id}/file',{'path':'Ref/viz-data.txt','content':'REMOTE'})
            if mode=='case_unavailable':await request(api,'POST',f'/fixture/block/{case_id}',api=False)
            if mode=='source_rebound':await request(api,'POST',f'/fixture/rebind/{sid}',api=False)
            async def close():return await request(api,'DELETE',f'/viz:sessions/{sid}',expect=200 if mode in {'success','sync_hold','cleanup_retry'} else 502)
            if mode=='sync_hold':
                operation=asyncio.create_task(close())
                await until(lambda:asyncio.to_thread((api.root/'sync-started').exists),bool)
                current=await request(api,'GET',f'/viz:sessions/{sid}')
                assert current['alive'] is False and current['finalizing'] is True and current['phase']=='active' and current['stop'] is None and current['ended_at'] is None,current
                assert not operation.done() and (remote/'Ref/viz-data.txt').read_text()=='BASE'
                await verify_liveness(api,case_id,'viz',boundary)
                (api.root/'release-sync').touch();await operation
            else:await close()
            result=(await request(api,'GET',f'/fixture/session/{sid}',api=False))['session']
            assert result['cleanup_verified'] is True and result['stop']['verified'] is True,result
            if mode in {'success','sync_hold','cleanup_retry'}:
                assert result['phase']=='closed' and (remote/'Ref/viz-data.txt').read_text()=='STAGED',result
                assert (remote/f".vibeflow/viz/viz-{session['port']}.log").stat().st_size>0
                assert not stage.exists() and not manifest.exists() and not marker.exists()
            elif mode=='cleanup_failure':
                assert result['phase']=='failed' and result['error']=='TerminalStagingCleanupError',result
                assert stage.exists() and manifest.exists() and marker.exists()
                assert json.loads(marker.read_text())['source_synchronized'] is True
                assert (remote/'Ref/viz-data.txt').read_text()=='STAGED'
                assert (await request(api,'POST',f'/fixture/retry/{sid}',api=False))['blocked'] is True
            else:
                assert result['phase']=='failed' and result['error']==('StagingConflict' if mode=='conflict' else 'CaseWriteConflict'),result
                assert stage.exists() and manifest.exists() and (stage/'Ref/viz-data.txt').read_text()=='STAGED'
                assert (remote/'Ref/viz-data.txt').read_text()==('REMOTE' if mode=='conflict' else 'BASE')
            checks.append(mode);runs.append({'mode':mode,'session':result,'snapshot':snapshot});print('VIZ_STAGING_OK '+mode,flush=True)
        for mode in ('cancel_clone','selection_commit_failure'):
            if selected and mode not in selected:continue
            case=await request(api,'POST',f"/workspaces/{workspace['id']}/cases",{'name':'viz '+mode});case_id=case['id']
            await request(api,'PUT',f'/cases/{case_id}/file',{'path':'D-post/cube.stl','content':STL})
            cloned=api.root/'cloned.json';cloned.unlink(missing_ok=True)
            before=(await request(api,'GET','/fixture/count',api=False))['count']
            await request(api,'POST','/fixture/mode/'+mode,api=False)
            task=asyncio.create_task(request(api,'POST',f'/cases/{case_id}/viz:sessions',{'file_path':'D-post/cube.stl'},expect=500,raw=True))
            await until(lambda:asyncio.to_thread(cloned.exists),bool)
            prefix=json.loads(cloned.read_text())['stage'];stage=api.root/'files'/prefix
            assert stage.is_relative_to(api.root/'files')
            if mode=='cancel_clone':
                cancelled=asyncio.create_task(request(api,'POST',f'/fixture/cancel/{case_id}',api=False))
                await until(lambda:asyncio.to_thread((api.root/'cancel-signalled').exists),bool)
                (api.root/'release-clone').touch();await cancelled
            await task;await request(api,'POST','/fixture/mode/normal',api=False)
            assert (await request(api,'GET','/fixture/count',api=False))['count']==before
            assert not stage.exists() and not (stage.parent/(stage.name+'.manifest.json')).exists()
            checks.append(mode);runs.append({'mode':mode,'no_owner_allocated':True});print('VIZ_STAGING_OK '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root),
        'real_vtk_kernel':True,'controlled_s3_facade':True,'actual_remote_s3_service_used':False}
    (server.root/'staging-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8');return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='viz-staging-evidence-',dir=SERVER.parents[1]/'tmp'));results={'sqlite':await verify()}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_vizstage_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_vizstage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10);created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head');results['postgresql']=await verify(temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres';await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('VIZ_STAGING_ALL_OK '+json.dumps({'databases':list(results),'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
