"""Real wmake/CDLL and immutable FloTHERM helper-library publication."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import IsolatedServer, SERVER
from professional_launch_smoke import settled
from job_release_smoke import wait_file
from event_replay_smoke import peer
from workflow_migration_smoke import alembic

MODES = ('copy', 'same', 'install', 'repeat', 'compile_fail', 'load_fail',
         'receipt', 'source', 'target', 'cancel', 'death')
DEST = 'UDF/flotherm-helper'
LIB = DEST+'/libflothermHelper.so'
SOURCE = 'extern "C" double vfFlothermPowerScale() { return 1.0; }\n'
FIXTURE = '''import time
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.flotherm_library_handler import FlothermLibraryHandler
root=Path(__file__).parent
original=publication.prepare_bundle
def hold(*args):
 if (root/'mode').read_text() in {'source','target','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('FloTHERM library publication fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold
artifacts=FlothermLibraryHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 if (root/'mode').read_text()=='receipt':(run/'.vibeflow/flotherm-library/receipt.json').write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
FlothermLibraryHandler.artifacts=verify
from app.main import app
'''


def seed(api, directory, install=False):
    api.put(directory+'/flothermHelper.C', SOURCE)
    api.put(directory+'/Make/files', 'flothermHelper.C\n\nLIB = $('+
            ('FOAM_USER_LIBBIN' if install else 'PWD')+')/libflothermHelper\n')
    api.put(directory+'/Make/options', 'EXE_INC =\nLIB_LIBS =\n')


def verify(database_url=None, modes=MODES):
    api = IsolatedServer('flotherm-library-publication', app='library_fixture:app', database_url=database_url)
    (api.root/'library_fixture.py').write_text(FIXTURE, encoding='utf8')
    (api.root/'mode').write_text('seed')
    checks = []
    with api:
        api.create_case('FloTHERM library donor')
        seed(api, DEST)
        donor = settled(api, api.call('POST', api.prefix+'/flotherm:build-library',
            {'source_dir': DEST, 'destination_dir': DEST})['job_id'])
        assert donor['status'] == 'succeeded', donor.get('log_backlog', '')
        donor_root = next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        previous = (donor_root/LIB).read_bytes()
        for mode in modes:
            api.create_case('FloTHERM library '+mode)
            root = next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            copied = mode in {'copy', 'install'}
            source = 'Ref/source %VALUE% & helper' if copied else DEST
            seed(api, source, install=(mode == 'install'))
            api.put('unrelated.txt', 'keep original')
            if not copied:
                (root/LIB).write_bytes(previous)
            if mode == 'compile_fail':
                api.put(source+'/flothermHelper.C', 'invalid C++ source\n')
            if mode == 'load_fail':
                api.put(source+'/flothermHelper.C', 'extern "C" double missing_dependency();\n'
                    'extern "C" double vfFlothermPowerScale() { return missing_dependency(); }\n')
            before = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root/source).rglob('*')
                      if p.is_file() and p.name != 'libflothermHelper.so'}
            for name in ('ready', 'release'):
                (api.root/name).unlink(missing_ok=True)
            (api.root/'mode').write_text(mode)
            body = {'source_dir': source, 'destination_dir': DEST}
            headers = {'Idempotency-Key': 'library-'+mode}
            identifier = api.call('POST', api.prefix+'/flotherm:build-library', body, extra_headers=headers)['job_id']
            if mode in {'source', 'target', 'cancel', 'death'}:
                wait_file(api.root/'ready', seconds=60)
                assert (root/LIB).read_bytes() == previous
                if mode == 'source':
                    (root/source/'flothermHelper.C').write_text(SOURCE+'// user edit\n')
                if mode == 'target':
                    (root/LIB).write_bytes(b'user output edit')
                if mode == 'cancel':
                    api.call('POST', f'/jobs/{identifier}:cancel')
                if mode == 'death':
                    api.proc.terminate()
                    api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done = recovered.wait_job(identifier, timeout=60)
                        assert done['attempt']['outputs']['publication']['status'] == 'committed'
                        assert (root/LIB).read_bytes()[:4] == b'\x7fELF'
                        assert len(recovered.call('GET', api.prefix+'/jobs')) == 1
                    checks.append('API_restart_publishes_original_validated_library_without_second_compile')
                    break
                (api.root/'release').touch()
            success = mode in {'copy', 'same', 'install', 'repeat'}
            done = settled(api, identifier)
            assert done['status'] == ('succeeded' if success else 'cancelled' if mode == 'cancel' else 'failed'), {
                'mode': mode, 'status': done['status'], 'log': done.get('log_backlog', '')[-2200:]}
            assert (root/'unrelated.txt').read_text() == 'keep original'
            for path, data in before.items():
                if mode == 'source' and path.endswith('flothermHelper.C'):
                    assert (root/path).read_text() == SOURCE+'// user edit\n'
                else:
                    assert (root/path).read_bytes() == data, path
            frozen = done['attempt']['workspace']
            private = root/frozen['run_path']
            assert '.vibeflow/prepared/flotherm-library.json' in frozen['generated_inputs']
            if mode in {'compile_fail','load_fail'}:
                transcript=(private/'.vibeflow/flotherm-library/native.log').read_text(encoding='utf8',errors='replace')
                assert ('undefined symbol: missing_dependency' if mode=='load_fail' else 'error:') in transcript
            for path, data in before.items():
                assert (root/frozen['inputs_path']/path).read_bytes() == data
            publication = done['attempt']['outputs']['publication']
            if not success:
                assert publication['status'] != 'committed'
                assert (root/LIB).read_bytes() == (b'user output edit' if mode == 'target' else previous)
                checks.append(mode+'_preserves_previous_library_and_original_inputs')
            else:
                assert publication['status'] == 'committed'
                bundle = root/'.vibeflow/publications'/publication['id']
                manifest = json.loads((bundle/'manifest.json').read_text())
                native = manifest['descriptor']['receipt']['native']
                assert native['uid'] != 0 and LIB in native['libraries']
                assert set(native['steps']) == {'build', 'load'}
                assert bool(native['private_installations']) == (mode == 'install')
                assert json.loads((private/'.vibeflow/flotherm-library/stage.json').read_text())['removed']
                child = list(done['attempt']['child_processes']['children'].values())[0]
                assert child['purpose'] == 'flotherm_library' and child['stop']['verified'] and child['stop']['native_state'] == 'exited'
                assert api.call('POST', api.prefix+'/flotherm:build-library', body, extra_headers=headers)['job_id'] == identifier
                if copied:
                    api.call('POST', api.prefix+'/flotherm:build-library', body, expect=409)
                reader = api.call('POST', api.prefix+'/jobs', {'command': 'echo LIBRARY_RESULT_OK',
                    'input_publications': [{'publication_id': publication['id'], 'require_current': True}]})
                api.wait_job(reader['id'])
                if mode == 'repeat':
                    first = (bundle/'files'/LIB).read_bytes()
                    api.put(DEST+'/flothermHelper.C', SOURCE.replace('1.0', '2.0'))
                    rebuilt = settled(api, api.call('POST', api.prefix+'/flotherm:build-library', body)['job_id'])
                    assert rebuilt['status'] == 'succeeded', rebuilt.get('log_backlog', '')[-2000:]
                    assert (root/LIB).read_bytes() != first and (bundle/'files'/LIB).read_bytes() == first
                    command = ('python3 -c \'import ctypes,os;lib=ctypes.CDLL(os.path.abspath("'+LIB+'"));'
                        'lib.vfFlothermPowerScale.restype=ctypes.c_double;assert lib.vfFlothermPowerScale()==2.0;print("REBUILT_LIBRARY_EXECUTED")\'')
                    called = settled(api, api.call('POST', api.prefix+'/jobs', {'command': command, 'runtime': 'wsl'})['id'])
                    assert called['status'] == 'succeeded' and 'REBUILT_LIBRARY_EXECUTED' in called['log_backlog']
                    checks.append('changed_CPP_recompiled_and_new_function_executed_old_bundle_preserved')
                checks.append(mode+'_actual_build_load_and_guarded_publication')
            print('PASS '+checks[-1], flush=True)
    return {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(api.root)}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--modes', nargs='+', choices=MODES)
    args = parser.parse_args()
    modes = tuple(args.modes) if args.modes else MODES
    report = Path(tempfile.mkdtemp(prefix='flotherm-library-evidence-', dir=SERVER.parents[1]/'tmp'))
    results = {'sqlite': await asyncio.to_thread(verify, None, modes)}
    (report/'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost', '127.0.0.1', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_flothermlibrary_'+uuid.uuid4().hex
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created = True
            temporary = url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, temporary, report, 'upgrade', 'head')
            results['postgresql'] = await asyncio.to_thread(verify, temporary, modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (report/'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('FLOTHERM_LIBRARY_PUBLICATION_OK '+json.dumps({'evidence': str(report), 'counts': {k:v['count'] for k,v in results.items()}}))


if __name__ == '__main__':
    asyncio.run(main())
