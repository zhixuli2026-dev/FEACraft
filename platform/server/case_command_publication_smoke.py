"""Typed raw commands: real execution, guarded publication and serial solve compatibility."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import IsolatedServer, SERVER
from default_workspace_smoke import command
from event_replay_smoke import peer
from job_release_smoke import wait_file
from professional_launch_smoke import settled
from workflow_migration_smoke import alembic

FIXTURE='''
import time
from pathlib import Path
from app.services import artifact_publication as publication
root=Path(__file__).parent
original=publication.prepare_bundle
def hold(*args):
 if (root/'mode').read_text() in {'source','destination','delete_conflict','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('typed command publication fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold
from app.main import app
'''


def verify(database_url=None):
    api=IsolatedServer('case-command-publication',app='case_command_fixture:app',database_url=database_url)
    (api.root/'case_command_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal');checks=[]
    with api:
        modes=('mesh','solve','geometry','pvpython','convert','wsl','noop','delete','failure','preserved',
               'source','destination','delete_conflict','cancel','timeout','death')
        for mode in modes:
            api.create_case('typed command '+mode)
            api.put('Ref/input.txt','original input')
            api.put('C-run/probe/0/U','initial field')
            api.put('C-run/probe/0.orig/U','original field')
            api.put('D-post/result.txt','previous result')
            api.put('D-post/obsolete.txt','previous obsolete')
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            (api.root/'mode').write_text(mode)
            code="from pathlib import Path; Path('D-post/result.txt').write_text(Path('Ref/input.txt').read_text()+' result')"
            if mode in {'delete','delete_conflict'}:code+="; Path('D-post/obsolete.txt').unlink()"
            if mode=='failure':code+='; raise SystemExit(7)'
            if mode=='preserved':code+="; Path('C-run/probe/0/U').write_text('unapproved initial field')"
            if mode=='timeout':code+='; import time; time.sleep(60)'
            if mode=='noop':code="print('TYPED_NO_OUTPUT')"
            body={'type':mode if mode in {'mesh','solve','geometry','pvpython','convert'} else 'convert',
                  'command':command(code)}
            if mode=='timeout':body['timeout_seconds']=3
            if mode=='wsl':body.update(runtime='wsl',command='printf "original input result" > D-post/result.txt')
            headers={'Idempotency-Key':'typed-'+mode}
            identifier=api.call('POST',api.prefix+'/jobs',body,extra_headers=headers)['id']
            if mode in {'source','destination','delete_conflict','cancel','death'}:
                wait_file(api.root/'ready',seconds=45)
                assert (root/'D-post/result.txt').read_text()=='previous result'
                if mode=='source':(root/'Ref/input.txt').write_text('new user input')
                if mode=='destination':(root/'D-post/result.txt').write_text('new user result')
                if mode=='delete_conflict':(root/'D-post/obsolete.txt').write_text('new user obsolete')
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=settled(recovered,identifier)
                        assert done['status']=='succeeded'
                        assert done['attempt']['outputs']['publication']['status']=='committed'
                        assert (root/'D-post/result.txt').read_text()=='original input result'
                        assert len(recovered.call('GET',api.prefix+'/jobs'))==1
                    checks.append('API_death_recovers_publication_without_command_replay');break
                (api.root/'release').touch()
            done=settled(api,identifier)
            success=mode in {'mesh','solve','geometry','pvpython','convert','wsl','noop','delete'}
            assert done['status']==('succeeded' if success else 'cancelled' if mode=='cancel' else 'failed'),(mode,done['status'],done['log_backlog'][-1500:])
            attempt=done['attempt'];frozen=attempt['workspace']
            inputs=root/frozen['inputs_path'];private=root/frozen['run_path']
            assert (inputs/'Ref/input.txt').read_text()=='original input'
            assert (inputs/'D-post/result.txt').read_text()=='previous result'
            assert (root/'C-run/probe/0/U').read_text()=='initial field'
            assert (root/'C-run/probe/0.orig/U').read_text()=='original field'
            publication=attempt['outputs']['publication']
            if not success:
                assert publication['status']!='committed'
                assert (root/'D-post/result.txt').read_text()==('new user result' if mode=='destination' else 'previous result')
                assert (root/'D-post/obsolete.txt').read_text()==('new user obsolete' if mode=='delete_conflict' else 'previous obsolete')
            else:
                assert publication['status']=='committed'
                bundle=root/'.vibeflow/publications'/publication['id']
                manifest=json.loads((bundle/'manifest.json').read_text())
                assert manifest['descriptor']['handler']=='case-command.v1'
                assert manifest['descriptor']['result']['professional_workflow_proof'] is False
                assert manifest['descriptor']['result']['legacy_solve_evidence'] is False
                if mode=='noop':assert manifest['files']=={}
                else:assert (root/'D-post/result.txt').read_bytes()==(private/'D-post/result.txt').read_bytes()==b'original input result'
                if mode=='delete':assert not (root/'D-post/obsolete.txt').exists()
                if mode=='convert':
                    reader=api.call('POST',api.prefix+'/jobs',{'command':'echo CURRENT_REFERENCE',
                        'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
                    assert settled(api,reader['id'])['status']=='succeeded'
                    api.put('Ref/input.txt','edited after publication')
                    assert api.call('POST',api.prefix+'/jobs',body,extra_headers=headers)['id']==identifier
                    api.call('POST',api.prefix+'/jobs',{'command':'echo STALE_REFERENCE',
                        'input_publications':[{'publication_id':publication['id'],'require_current':True}]},expect=409)
                    assert (bundle/'files/D-post/result.txt').read_bytes()==b'original input result'
            checks.append(mode+'_isolated_publication');print('PASS '+checks[-1],flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='case-command-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_typed_'+uuid.uuid4().hex
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
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('CASE_COMMAND_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
