"""Real FloTHERM conversion and guarded publication on disposable APIs/databases."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import uuid

from testsupport import IsolatedServer, SERVER
from app.services.flotherm_ir import build_ir
from professional_launch_smoke import settled
from job_release_smoke import wait_file
from event_replay_smoke import peer
from workflow_migration_smoke import alembic

MODES = ('normal', 'settings', 'unsupported', 'invalid_ir', 'invalid_settings', 'receipt',
         'artifact', 'source', 'target', 'cancel', 'death')
FIXTURE = '''import time
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.flotherm_convert_handler import FlothermConvertHandler
root=Path(__file__).parent
original=publication.prepare_bundle
def hold(*args):
 if (root/'mode').read_text() in {'source','target','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+60
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('FloTHERM publication fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold
artifacts=FlothermConvertHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 mode=(root/'mode').read_text()
 if mode=='receipt':(run/'.vibeflow/flotherm-convert/receipt.json').write_text('{}')
 if mode=='artifact':(run/descriptor['output_dir']/'constant/fvOptions').write_text('corrupted physics')
 return artifacts(self,run,descriptor,output,frozen)
FlothermConvertHandler.artifacts=verify
from app.main import app
'''


def input_ir():
    return build_ir('source.ecxml', b'chip', [{'id': 'chip', 'type': 'box', 'material': 'silicon',
        'mapping': 'converted', 'power_w': 5, 'shape': {'kind': 'box', 'origin': [0, 0, 0],
        'dimensions': [1, 1, 1]}}], {'silicon': {'conductivity_w_mk': 130, 'rho_cp': 1000}})


def verify(database_url=None, modes=MODES):
    api = IsolatedServer('flotherm-publication', app='flotherm_fixture:app', database_url=database_url)
    (api.root/'flotherm_fixture.py').write_text(FIXTURE, encoding='utf8')
    (api.root/'mode').write_text('normal')
    checks = []
    with api:
        for mode in modes:
            api.create_case('FloTHERM ' + mode)
            root = next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            ir = input_ir()
            if mode == 'unsupported':
                ir['materials']['silicon']['conductivity_w_mk'] = [130, 130, 100]
            if mode == 'invalid_ir':
                ir['schema'] = 'invalid'
            raw = json.dumps(ir)
            api.put('Ref/ir.json', raw)
            api.put('unrelated.txt', 'keep user original')
            # Spaces and shell metacharacters remain literal argv paths.
            output = 'C-run/thermal %VALUE% & output' if mode == 'settings' else 'C-run/thermal'
            body = {'ir_path': 'Ref/ir.json', 'output_dir': output}
            if mode in {'settings', 'invalid_settings'}:
                settings = {'power': {'chip': {'value': 7500, 'unit': 'mW'}}} if mode == 'settings' else {'unsupported_setting': 1}
                api.put('Ref/settings.json', json.dumps(settings))
                body['settings_path'] = 'Ref/settings.json'
            # Existing empty output directories are supported and preserved.
            (root/output).mkdir(parents=True)
            for name in ('ready', 'release'):
                (api.root/name).unlink(missing_ok=True)
            (api.root/'mode').write_text(mode)
            headers = {'Idempotency-Key': 'flotherm-' + mode}
            identifier = api.call('POST', api.prefix+'/flotherm:convert', body, extra_headers=headers)['job_id']
            if mode in {'source', 'target', 'cancel', 'death'}:
                wait_file(api.root/'ready', seconds=40)
                assert list((root/output).iterdir()) == [], 'Published files before validation'
                if mode == 'source':
                    (root/'Ref/ir.json').write_text(raw+'\n', encoding='utf8')
                if mode == 'target':
                    (root/output/'user.txt').write_text('user edit')
                if mode == 'cancel':
                    api.call('POST', f'/jobs/{identifier}:cancel')
                if mode == 'death':
                    api.proc.terminate()
                    api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done = recovered.wait_job(identifier, timeout=60)
                        assert done['attempt']['outputs']['publication']['status'] == 'committed'
                        assert (root/output/'constant/fvOptions').is_file()
                        assert len(recovered.call('GET', api.prefix+'/jobs')) == 1
                    checks.append('API_restart_publishes_validated_conversion_without_reexecution')
                    break
                (api.root/'release').touch()
            success = mode in {'normal', 'settings', 'unsupported'}
            done = settled(api, identifier)
            assert done['status'] == ('succeeded' if success else 'cancelled' if mode == 'cancel' else 'failed'), {
                'mode': mode, 'status': done['status'], 'log': done.get('log_backlog', '')[-2000:]}
            assert (root/'unrelated.txt').read_text() == 'keep user original'
            assert (root/'Ref/ir.json').read_text() == raw+('\n' if mode == 'source' else '')
            frozen = done['attempt']['workspace']
            assert (root/frozen['inputs_path']/'Ref/ir.json').read_text() == raw
            assert '.vibeflow/prepared/flotherm-convert.json' in frozen['generated_inputs']
            publication = done['attempt']['outputs']['publication']
            if not success:
                assert publication['status'] != 'committed'
                assert sorted(p.name for p in (root/output).iterdir()) == (['user.txt'] if mode == 'target' else [])
                checks.append(mode+'_preserves_live_inputs_and_output_directory')
            else:
                assert publication['status'] == 'committed'
                bundle = root/'.vibeflow/publications'/publication['id']
                manifest = json.loads((bundle/'manifest.json').read_text())
                assert manifest['descriptor']['receipt']['attempt_id'] == done['attempt']['id']
                assert manifest['descriptor']['deleted_files'] == {}
                generated = {p.relative_to(root/output).as_posix() for p in (root/output).rglob('*') if p.is_file()}
                marker = json.loads((root/output/'.flotherm2foam-generated').read_text())
                assert set(marker['files']) == generated-{'.flotherm2foam-generated'}
                assert generated == {str(Path(p).relative_to(output)).replace('\\', '/') for p in manifest['files']}
                for path in manifest['files']:
                    assert (root/path).read_bytes() == (bundle/'files'/path).read_bytes()
                coverage = json.loads((root/output/'conversion-coverage.json').read_text())
                assert coverage['summary']['input_power_w'] == (7.5 if mode == 'settings' else 5)
                preflight = json.loads((root/output/'preflight.json').read_text())
                assert preflight['ok'] == (mode != 'unsupported')
                if mode == 'unsupported':
                    assert preflight['blockers'], 'Unsupported physics must not be declared runnable'
                assert api.call('POST', api.prefix+'/flotherm:convert', body, extra_headers=headers)['job_id'] == identifier
                api.call('POST', api.prefix+'/flotherm:convert', body, expect=409)
                reader = api.call('POST', api.prefix+'/jobs', {'command': 'echo FLOTHERM_RESULT_OK',
                    'input_publications': [{'publication_id': publication['id'], 'require_current': True}]})
                api.wait_job(reader['id'])
                checks.append(mode+'_complete_conversion_immutable_bundle_and_current_reader')
            print('PASS '+checks[-1], flush=True)
    return {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(api.root)}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--modes', nargs='+', choices=MODES)
    args = parser.parse_args()
    modes = tuple(args.modes) if args.modes else MODES
    report = Path(tempfile.mkdtemp(prefix='flotherm-publication-evidence-', dir=SERVER.parents[1]/'tmp'))
    results = {'sqlite': await asyncio.to_thread(verify, None, modes)}
    (report/'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost', '127.0.0.1', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_flothermpublication_' + uuid.uuid4().hex
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
    print('FLOTHERM_PUBLICATION_OK '+json.dumps({'evidence': str(report), 'counts': {k:v['count'] for k,v in results.items()}}))


if __name__ == '__main__':
    asyncio.run(main())
