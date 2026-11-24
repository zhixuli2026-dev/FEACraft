"""Actual UDF build/load/solve, preserved inputs and atomic control/result publication."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import IsolatedServer,SERVER
from mpi_publication_smoke import serial_seed,compare,RUN
from professional_launch_smoke import settled
from job_release_smoke import wait_file
from event_replay_smoke import peer
from workflow_migration_smoke import alembic

UDF='UDF/myFunctionObject';LIB=UDF+'/libmyFunctionObject.so'
MODES=('build_normal','build_repeat','build_compile_fail','build_receipt','build_source','build_target','build_cancel',
    'integrate_normal','integrate_bad_library','integrate_solver_fail','integrate_control','integrate_source',
    'integrate_target','integrate_cancel','integrate_death')
FIXTURE='''import time
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.udf_handler import UdfHandler
root=Path(__file__).parent
original=publication.prepare_bundle
def hold(*args):
 mode=(root/'mode').read_text()
 if mode.endswith(('_source','_target','_cancel','_control','_death')):
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('UDF publication fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold
artifacts=UdfHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 if (root/'mode').read_text()=='build_receipt':(run/'.vibeflow/udf/receipt.json').write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
UdfHandler.artifacts=verify
from app.main import app
'''


def verify(database_url=None,modes=MODES):
    api=IsolatedServer('udf-publication',app='udf_fixture:app',database_url=database_url)
    (api.root/'udf_fixture.py').write_text(FIXTURE,encoding='utf8');(api.root/'mode').write_text('seed')
    checks=[];errors=[]
    with api:
        seed,serial=serial_seed(api)
        scaffold=api.call('POST',api.prefix+'/udf/scaffold',{})
        donor=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        sources={UDF+'/'+p:(donor/UDF/p).read_bytes() for p in scaffold['files']}
        api.wait_job(api.call('POST',api.prefix+'/udf:build',{})['job_id'])
        library=(donor/LIB).read_bytes();assert library[:4]==b'\x7fELF'
        for mode in modes:
            operation=mode.split('_',1)[0];api.create_case('UDF '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            for path,data in {**seed,**sources,LIB:library}.items():
                target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            control=root/RUN/'system/controlDict'
            if mode=='build_compile_fail':(root/UDF/'myFunctionObject.C').write_text('invalid C++ source\n')
            if mode=='integrate_bad_library':(root/LIB).write_bytes(b'not an ELF library')
            if mode=='integrate_solver_fail':
                (root/RUN/'system/fvSolution').write_text('invalid solver dictionary\n')
            target=root/LIB if operation=='build' else root/RUN/'0.5/U'
            if operation=='integrate':target.parent.mkdir(exist_ok=True);target.write_bytes(b'previous U field')
            old_target=target.read_bytes();old_control=control.read_bytes()
            before_source=(root/UDF/'myFunctionObject.C').read_bytes()
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            body={} if operation=='build' else {'run_dir':RUN}
            headers={'Idempotency-Key':mode}
            identifier=api.call('POST',api.prefix+'/udf:'+operation,body,extra_headers=headers)['job_id']
            assert control.read_bytes()==old_control,'Integration changed live controlDict before completion'
            if mode.endswith(('_source','_target','_cancel','_control','_death')):
                wait_file(api.root/'ready',seconds=90)
                assert target.read_bytes()==old_target and control.read_bytes()==old_control
                if mode.endswith('_source'):(root/UDF/'myFunctionObject.H').write_bytes(sources[UDF+'/myFunctionObject.H']+b'\n// user edit\n')
                if mode.endswith('_target'):target.write_bytes(b'user output edit')
                if mode.endswith('_control'):control.write_bytes(old_control+b'\n// user control edit\n')
                if mode.endswith('_cancel'):api.call('POST',f'/jobs/{identifier}:cancel')
                if mode.endswith('_death'):
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=60)
                        assert done['attempt']['outputs']['publication']['status']=='committed'
                        assert control.read_bytes()!=old_control and b'myFunctionObject' in control.read_bytes()
                        errors.append(compare(serial,target.read_bytes()))
                        assert len(recovered.call('GET',api.prefix+'/jobs'))==1
                    checks.append('API_death_recovers_validated_UDF_integration_without_second_solve');break
                (api.root/'release').touch()
            success=mode in {'build_normal','build_repeat','integrate_normal'}
            done=settled(api,identifier)
            assert done['status']==('succeeded' if success else 'cancelled' if mode.endswith('_cancel') else 'failed'),{
                'mode':mode,'status':done['status'],'log':done.get('log_backlog','')[-2500:]}
            assert (root/UDF/'myFunctionObject.C').read_bytes()==before_source
            frozen=done['attempt']['workspace'];private=root/frozen['run_path'];inputs=root/frozen['inputs_path']
            assert (inputs/RUN/'system/controlDict').read_bytes()==old_control
            assert '.vibeflow/prepared/udf.json' in frozen['generated_inputs']
            publication=done['attempt']['outputs']['publication']
            if not success:
                assert publication['status']!='committed'
                assert target.read_bytes()==(b'user output edit' if mode.endswith('_target') else old_target)
                assert control.read_bytes()==(old_control+b'\n// user control edit\n' if mode.endswith('_control') else old_control)
                checks.append(mode+'_preserves_live_source_control_and_previous_outputs')
            else:
                assert publication['status']=='committed'
                bundle=root/'.vibeflow/publications'/publication['id']
                manifest=json.loads((bundle/'manifest.json').read_text())
                receipt=manifest['descriptor']['receipt'];native=receipt['native']
                assert native['uid']!=0 and native['mode']==operation and native['library']['size']>1000
                assert receipt['attempt_id']==done['attempt']['id']
                assert json.loads((private/'.vibeflow/udf/stage.json').read_text())['removed']
                children=list(done['attempt']['child_processes']['children'].values())
                assert len(children)==1 and children[0]['purpose']=='udf_run' and children[0]['phase']=='stopped'
                assert children[0]['stop']['verified'] and children[0]['stop']['native_state']=='exited'
                assert api.call('POST',api.prefix+'/udf:'+operation,body,extra_headers=headers)['job_id']==identifier
                reader=api.call('POST',api.prefix+'/jobs',{'command':'echo UDF_RESULT_OK',
                    'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
                api.wait_job(reader['id'])
                if operation=='integrate':
                    errors.append(compare(serial,target.read_bytes()))
                    assert b'myFunctionObject' in control.read_bytes() and 'P0_UDF_OK' in (bundle/'files'/RUN/'udf-solve.log').read_text()
                    api.call('POST',api.prefix+'/udf:integrate',body,expect=409)
                else:assert control.read_bytes()==old_control and target.read_bytes()[:4]==b'\x7fELF'
                if mode=='build_repeat':
                    old_library=(bundle/'files'/LIB).read_bytes()
                    source=(root/UDF/'myFunctionObject.C').read_text().replace('P0_UDF_OK','UDF_REBUILT_OK')
                    assert 'UDF_REBUILT_OK' in source;api.put(UDF+'/myFunctionObject.C',source)
                    rebuilt=api.wait_job(api.call('POST',api.prefix+'/udf:build',{})['job_id'])
                    assert rebuilt['attempt']['outputs']['publication']['status']=='committed'
                    assert target.read_bytes()!=old_library and (bundle/'files'/LIB).read_bytes()==old_library
                    (api.root/'mode').write_text('integration_after_rebuild')
                    integrated=api.wait_job(api.call('POST',api.prefix+'/udf:integrate',{'run_dir':RUN})['job_id'])
                    transcript=(root/RUN/'udf-solve.log').read_text()
                    assert 'UDF_REBUILT_OK' in transcript and 'P0_UDF_OK' not in transcript
                    checks.append('edited_CPP_really_recompiles_and_executes_new_library_preserving_history')
                checks.append(mode+'_real_toolchain_and_guarded_publication')
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'serial_U_relative_errors':errors,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+',choices=MODES)
    args=parser.parse_args();modes=tuple(args.modes) if args.modes else MODES
    report=Path(tempfile.mkdtemp(prefix='udf-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,None,modes)}
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_udfpublication_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary,modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('UDF_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
