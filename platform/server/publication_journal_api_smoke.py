"""Real Pandoc jobs and SQL/file journal diagnosis across commit failure/API death."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import IsolatedServer,SERVER
from publication_copy_smoke import CONTROLLED,done
from job_release_smoke import wait_file
from job_launch_lease_smoke import PEER,settled
from workflow_migration_smoke import alembic


def verify(database_url=None):
    checks=[];evidence=[]
    for mode in ('normal','commit_error','api_death'):
        api=IsolatedServer('publication-journal-'+mode,app='journal_fixture:app',database_url=database_url)
        (api.root/'journal_fixture.py').write_text(CONTROLLED,encoding='utf8')
        (api.root/'control.json').write_text(json.dumps({'stage':'none' if mode=='normal' else 'prepared','action':mode}),encoding='utf8')
        with api:
            case=api.create_case('publication journal '+mode)
            api.put('E-report/report.md','# Journal actual report\n\nPreserve completed artifacts.\n')
            submitted=api.call('POST',api.prefix+'/report:build',{})
            job_id=submitted['job_id']
            if mode!='normal':
                wait_file(api.root/'blocked.ready',seconds=90)
                if mode=='api_death':
                    observed=api.call('GET',f'/fixture/state/{job_id}')
                    assert observed['stop']['verified'] and observed['publication_status']=='prepared',observed
                    api.proc.terminate();api.proc.wait(timeout=10)
                else:
                    (api.root/'blocked.continue').touch()
                    observed=done(api,job_id)
                    assert observed['publication_status']=='prepared',observed
            else:
                observed=done(api,job_id)
                assert observed['publication_status']=='committed',observed
            peer=IsolatedServer('publication-journal-reader',app='journal_peer:app',database_url=database_url)
            overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
            (peer.root/'journal_peer.py').write_text(PEER.replace('OVERRIDES',repr(overrides)),encoding='utf8')
            with peer:
                peer.token=api.token
                if mode=='api_death':settled(peer,job_id)
                diagnostic=peer.call('GET',f'/jobs/{job_id}/publication-recovery')
                assert diagnostic['state']=='bundle_verified' and diagnostic['automatic_replay'] is False,diagnostic
                committed=mode in {'normal','api_death'}
                assert diagnostic['database_status']==('committed' if committed else 'prepared'),diagnostic
                assert diagnostic['next_step']==('retain_committed' if committed else 'retain_for_finalizer'),diagnostic
                detail=peer.call('GET',f'/jobs/{job_id}')
                assert detail['status']==('succeeded' if committed else 'running'),detail
                assert peer.call('GET',f'/jobs/{job_id}/publication-recovery')==diagnostic
                checks.append(mode+'_real_report_and_peer_diagnosis')
                if mode=='normal':
                    owner=peer.token
                    peer.create_case('not publication owner')
                    peer.call('GET',f'/jobs/{job_id}/publication-recovery',expect=(403,404))
                    peer.token=None
                    peer.call('GET',f'/jobs/{job_id}/publication-recovery',expect=401)
                    peer.token=owner
                    checks.append('diagnostic_owner_and_anonymous_access')
                evidence.append({'mode':mode,'api':str(api.root),'peer':str(peer.root),'diagnostic':diagnostic})
    return {'passed':True,'count':len(checks),'checks':checks,'runs':evidence}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='publication-journal-api-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_pubjournal_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('PUBLICATION_JOURNAL_API_OK '+json.dumps({'databases':list(results),'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
