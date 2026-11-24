"""Real report process completion, interrupted alias writes and API restart."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from job_release_smoke import wait_file
from job_launch_lease_smoke import PEER,settled
from workflow_migration_smoke import alembic

CONTROLLED='''
import json,time
from pathlib import Path
from app import storage
from app.services import artifact_publication as pub
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
def hold():
    (root/'ready').touch()
    deadline=time.monotonic()+120
    while not (root/'release').exists():
        if time.monotonic()>deadline:raise TimeoutError('alias fixture release')
        time.sleep(.02)
put=storage._fs_put
def writing(prefix,path,data,*args,**kwargs):
    put(prefix,path,data,*args,**kwargs)
    if mode() in {'after_one','edited','newer','live_owner'} and path=='E-report/build/report.html' and not (root/'ready').exists():hold()
storage._fs_put=writing
aliases=pub._write_aliases
def applying(prefix,row):
    if mode()=='before_apply':hold()
    result=aliases(prefix,row)
    if mode()=='after_apply':hold()
    return result
pub._write_aliases=applying
from app.main import app
'''


def peer(api,disabled=False):
    server=IsolatedServer('alias-recovery-peer',app='alias_peer:app',database_url=api.database_url)
    overrides={k:api.env[k] for k in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
    script=PEER.replace('OVERRIDES',repr(overrides))
    if disabled:
        script+='\nfrom app.services import publication_alias_recovery\nasync def disabled(job_id):return None\npublication_alias_recovery.reconcile=disabled\n'
    (server.root/'alias_peer.py').write_text(script,encoding='utf8')
    server.token=api.token
    return server


MODES=('before_apply','after_one','after_apply','edited','newer','live_owner')


def verify(database_url=None,modes=MODES):
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('alias-recovery-'+mode,app='alias_fixture:app',database_url=database_url)
        (api.root/'alias_fixture.py').write_text(CONTROLLED,encoding='utf8')
        (api.root/'mode').write_text(mode)
        with api:
            case=api.create_case('alias recovery '+mode)
            api.put('E-report/report.md','# Original report\n\nImmutable output survives recovery.\n')
            submitted=api.call('POST',api.prefix+'/report:build',{})
            job_id=submitted['job_id'];publication=submitted['publication_id']
            wait_file(api.root/'ready',seconds=90)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{case["id"]}'))
            bundle=root/'.vibeflow/publications'/publication
            expected={p:(bundle/'files'/p).read_bytes() for p in ('E-report/build/report.html','E-report/build/report.docx')}
            if mode=='live_owner':
                started=time.monotonic()
                with peer(api) as reader:
                    assert time.monotonic()-started<12,'Peer startup waited on live alias writer transaction'
                    row=reader.call('GET',f'/jobs/{job_id}')
                    assert row['status']=='running' and row['completion_pending'],row
                (api.root/'release').touch();settled(api,job_id)
                checks.append('live_owner_not_taken_over_and_peer_startup_responsive')
                continue
            api.proc.terminate();api.proc.wait(timeout=10)
            original_mtimes={p:(root/p).stat().st_mtime_ns for p in expected if (root/p).exists()}
            if mode=='edited':(root/'E-report/build/report.html').write_text('USER EDIT',encoding='utf8')
            if mode=='newer':
                with peer(api,disabled=True) as writer:
                    writer.call('PUT',api.prefix+'/file',{'path':'E-report/report.md','content':'# A newer report\n'})
                    writer.call('PUT',api.prefix+'/file',{'path':'E-report/build/report.html','content':'USER DRAFT'})
            with peer(api) as reader:
                row=reader.call('GET',f'/jobs/{job_id}')
                if mode=='edited':
                    assert row['status']=='running' and row['completion_pending'],row
                    assert row['recovery']['state']=='requires_attention',row
                    assert (root/'E-report/build/report.html').read_text()=='USER EDIT'
                else:
                    assert row['status']=='succeeded' and not row['completion_pending'],row
                    if mode=='newer':
                        assert (root/'E-report/build/report.html').read_text()=='USER DRAFT'
                        newer=reader.call('POST',api.prefix+'/report:build',{})
                        settled(reader,newer['job_id'])
                        expected={p:(root/p).read_bytes() for p in expected}
                    assert all((root/p).read_bytes()==data for p,data in expected.items())
                    if mode=='after_apply':
                        assert all((root/p).stat().st_mtime_ns==stamp for p,stamp in original_mtimes.items())
                    raw=reader.call('GET',f'/jobs/{job_id}/log-stream',raw=True).decode()
                    assert '"type": "done"' in raw and '"status": "succeeded"' in raw,raw[-1000:]
                runs.append({'mode':mode,'api':str(api.root),'peer':str(reader.root),'job_id':job_id,'status':row['status']})
            # A second restart must preserve both files and terminal decisions.
            before={p:(root/p).read_bytes() for p in expected if (root/p).exists()}
            with peer(api) as again:
                repeated=again.call('GET',f'/jobs/{job_id}')
                assert repeated['status']==row['status']
                assert all((root/p).read_bytes()==data for p,data in before.items())
            checks.append(mode+'_restart_preserves_outputs_and_completion_contract')
    return {'passed':True,'count':len(checks),'checks':checks,'runs':runs}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--modes',nargs='+',choices=MODES,default=list(MODES));args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='alias-recovery-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,modes=args.modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_alias_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary,args.modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('ALIAS_RECOVERY_OK '+json.dumps({'databases':list(results),'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
