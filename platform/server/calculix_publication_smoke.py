"""Real CCX dependencies, immutable results, native ownership and interrupted publication."""
import argparse
import ast
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from testsupport import IsolatedServer, SERVER
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from event_replay_smoke import peer

DECK=next(ast.literal_eval(node.value) for node in ast.parse((SERVER/'calculix_smoke.py').read_text(encoding='utf8')).body
    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DECK' for t in node.targets))
PATH='C-run/calculix/beam.inp'
CONFIG='.vibeflow/prepared/calculix.json'
FIXTURE='''
import time
import sys
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.calculix_handler import CalculixHandler
from app.services.native_execution import NativeExecutor
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
original=publication.prepare_bundle
def hold_copy(*args):
 if mode() in {'source','destination','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('CalculiX copy fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold_copy
artifacts=CalculixHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 if mode()=='bad_receipt':(run/'.vibeflow/calculix/receipt.json').write_text('{}')
 if mode()=='bad_config':(run/'.vibeflow/prepared/calculix.json').write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
CalculixHandler.artifacts=verify
command=NativeExecutor._command
def invoke(self,purpose,parameters):
 result=command(self,purpose,parameters)
 if self.kind=='calculix' and mode()=='noop':
  return [sys.executable,'-c','print("Job finished")'],10,65536
 return result
NativeExecutor._command=invoke
from app.main import app
'''


def displacement(data):
    lines=data.decode('ascii').splitlines();active=False;values={}
    for line in lines:
        if 'DISP' in line:active=True;continue
        if active and line.startswith(' -3'):break
        if active and line.startswith(' -1'):
            values[int(line[3:13])]=[float(v) for v in re.findall(r'[+-]?\d+\.\d+E[+-]\d+',line[13:])]
    assert set(values)==set(range(1,9)),values
    assert all(max(map(abs,values[n]))<1e-12 for n in range(1,5)),values
    # Four equal -1000 loads, E=210000, unit cube: independently bounded
    # negative displacement; the restrained Poisson contraction stiffens it.
    assert all(-.02<values[n][2]<-.01 for n in range(5,9)),values
    assert max(values[n][2] for n in range(5,9))-min(values[n][2] for n in range(5,9))<1e-8


def verify(database_url=None):
    api=IsolatedServer('calculix-publication',app='calculix_fixture:app',database_url=database_url)
    (api.root/'calculix_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal');checks=[]
    with api:
        for mode in ('normal','restart','frequency','spaces','invalid','noop','bad_config','bad_receipt','source','destination','cancel','death'):
            api.create_case('CalculiX '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            path='C-run/space folder/beam model.inp' if mode=='spaces' else PATH
            if mode=='frequency':
                deck=DECK[:DECK.index('*STEP')].replace('*ELASTIC','*DENSITY\n7800\n*ELASTIC')+'*STEP\n*FREQUENCY,STORAGE=YES\n3\n*NODE FILE\nU\n*END STEP\n'
            else:deck=DECK.replace('*END STEP','*RESTART,WRITE\n*END STEP') if mode=='restart' else DECK
            api.put('Ref/model.inc',deck)
            api.put('Ref/nested.inc','*INCLUDE, INPUT=Ref/model.inc\n')
            api.put(path,'*INCLUDE, INPUT="Ref/nested.inc"\n')
            target=root/(path[:-4]+'.frd');target.write_bytes(b'previous result')
            (root/(path[:-4]+'.notes')).write_text('user notes')
            if mode=='invalid':
                before=api.call('GET',api.prefix+'/jobs')
                for text,status in [('*INCLUDE,INPUT=Ref/missing.inc\n',404),('*INCLUDE,INPUT=../outside.inp\n',400),
                        ('*INCLUDE,INPUT=Ref/nested.inc\n',400)]:
                    api.put('Ref/nested.inc',text)
                    api.call('POST',api.prefix+'/calculix:solve',{'input_path':path},expect=status)
                assert api.call('GET',api.prefix+'/jobs')==before
                assert target.read_bytes()==b'previous result'
                checks.append('missing_escape_and_cyclic_include_fail_before_job');continue
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            body={'input_path':path};headers={'Idempotency-Key':'calculix-'+mode}
            created=api.call('POST',api.prefix+'/calculix:solve',body,extra_headers=headers)
            identifier=created['job_id']
            if mode in {'source','destination','cancel','death'}:
                wait_file(api.root/'ready',seconds=30)
                assert target.read_bytes()==b'previous result'
                if mode=='source':(root/'Ref/model.inc').write_text(deck+'** changed\n')
                if mode=='destination':target.write_bytes(b'user edit')
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=45)
                        assert done['attempt']['outputs']['publication']['status']=='committed'
                        displacement(target.read_bytes())
                        assert len(recovered.call('GET',api.prefix+'/jobs'))==1
                    checks.append('API_death_recovers_validated_CCX_without_second_solve');break
                (api.root/'release').touch()
            success=mode in {'normal','restart','frequency','spaces'}
            done=api.wait_job(identifier,expected='succeeded' if success else 'cancelled' if mode=='cancel' else 'failed',timeout=40)
            if mode=='cancel':
                deadline=time.monotonic()+20
                while done['attempt']['phase']!='cancelled' and time.monotonic()<deadline:
                    time.sleep(.05);done=api.call('GET',f'/jobs/{identifier}')
                assert done['attempt']['phase']=='cancelled'
            frozen=done['attempt']['workspace'];private=root/frozen['run_path'];inputs=root/frozen['inputs_path']
            assert not (inputs/(path[:-4]+'.frd')).exists()
            assert (inputs/'Ref/model.inc').read_text()==deck
            assert CONFIG in frozen['generated_inputs'] and not (root/CONFIG).exists()
            assert (root/(path[:-4]+'.notes')).read_text()=='user notes'
            publication=done['attempt']['outputs']['publication']
            if not success:
                assert publication['status']!='committed'
                assert target.read_bytes()==(b'user edit' if mode=='destination' else b'previous result')
                checks.append(mode+'_cannot_publish_or_overwrite');continue
            assert publication['status']=='committed'
            assert target.read_bytes()==(private/(path[:-4]+'.frd')).read_bytes()
            bundle=root/'.vibeflow/publications'/publication['id']
            manifest=json.loads((bundle/'manifest.json').read_text())
            assert manifest['descriptor']['receipt']['attempt_id']==done['attempt']['id']
            assert manifest['descriptor']['receipt']['returncode']==0
            assert set(manifest['descriptor']['source']['files'])=={path,'Ref/model.inc','Ref/nested.inc'}
            children=done['attempt']['child_processes']['children']
            assert len(children)==1 and all(c['purpose']=='calculix_solve' and c['phase']=='stopped' and c['stop']['verified'] for c in children.values())
            assert api.call('POST',api.prefix+'/calculix:solve',body,extra_headers=headers)['job_id']==identifier
            reader=api.call('POST',api.prefix+'/jobs',{'command':'echo CCX_REFERENCE_OK',
                'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
            assert 'CCX_REFERENCE_OK' in api.wait_job(reader['id'])['log_backlog']
            if mode!='frequency':displacement(target.read_bytes())
            if mode=='frequency':assert (root/(path[:-4]+'.eig')).stat().st_size>0
            if mode=='normal':
                api.put('Ref/model.inc',deck+'** changed after publication\n')
                api.call('POST',api.prefix+'/jobs',{'command':'echo STALE',
                    'input_publications':[{'publication_id':publication['id'],'require_current':True}]},expect=409)
                checks.append('changed_include_invalidates_current_result_reference')
            if mode=='restart':
                restart=(root/(path[:-4]+'.rout')).read_bytes();assert restart
                (root/(path[:-4]+'.rin')).write_bytes(restart)
                api.put(path,'*RESTART,READ\n*STEP\n*STATIC\n*CLOAD\n5,3,-2000\n6,3,-2000\n7,3,-2000\n8,3,-2000\n*NODE FILE\nU\n*END STEP\n')
                second=api.wait_job(api.call('POST',api.prefix+'/calculix:solve',body)['job_id'])
                assert second['attempt']['outputs']['publication']['status']=='committed'
                assert (root/second['attempt']['workspace']['inputs_path']/(path[:-4]+'.rin')).read_bytes()==restart
                assert (bundle/'files'/(path[:-4]+'.frd')).read_bytes()!=(root/(path[:-4]+'.frd')).read_bytes()
                reader=api.call('POST',api.prefix+'/jobs',{'command':'echo CCX_HISTORY_OK',
                    'input_publications':[{'publication_id':publication['id'],'require_current':False}]})
                api.wait_job(reader['id'])
                checks.append('real_restart_read_write_and_immutable_old_result')
            checks.append(mode+'_native_solve_sealed_dependencies_owned_child_and_current_publication')
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='calculix-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_calculix_'+uuid.uuid4().hex
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
    print('CALCULIX_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
