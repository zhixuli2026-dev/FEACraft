"""Windows-owned batch launcher checks with non-numerical artifacts and no models."""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.services.process_identity_native import observe, probe
from testsupport import IsolatedServer


ABAQUS_ARGS = ['analysis', 'job=result', 'input=model.inp', 'cpus=1',
               'resultsformat=odb', 'interactive', 'scratch=.']
COMSOL_ARGS = ['-np', '1', '-prefsdir', 'prefs', '-configuration', 'config',
               '-data', 'workspace', '-tmpdir', 'scratch', '-recoverydir', 'recovery',
               '-mode', 'batch', '-inputfile', 'model.mph', '-outputfile', 'result.mph',
               '-batchlog', 'batch.log', '-batchlogout', '-study', 'std1']
ABAQUS_PRODUCTS = {'result.odb': b'Non-numerical Abaqus protocol artifact',
                   'result.sta': b'THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n'}
COMSOL_PRODUCTS = {'result.mph': b'Non-numerical COMSOL protocol artifact',
                   'batch.log': b'COMSOL launch protocol only\n'}
FLUENT_ARGS = ['3ddp', '-g', '-t1', '-wait', '-i', 'solve.jou']
STARCCM_ARGS = ['-np', '1', '-nbuserdir', 'profile', '-batch', 'run,SaveVibeFlowResult.java', 'model.sim']
HYPERMESH_ARGS = ['-tcl', 'export.tcl', '-nocommand']
HDF5 = b'\x89HDF\r\n\x1a\n'
FLUENT_PRODUCTS = {'result.cas.h5': HDF5 + b'Non-numerical Fluent case protocol artifact',
                   'result.dat.h5': HDF5 + b'Non-numerical Fluent data protocol artifact',
                   'fluent.log': b'Fluent launch protocol only\n'}
STARCCM_PRODUCTS = {'result.sim': b'Non-numerical STAR-CCM+ protocol artifact'}
HYPERMESH_PRODUCTS = {'result.fem': b'$ Test-only HyperMesh deck\n',
                      'mesh-summary.json': b'{"nodes":4,"elements":1}\n'}
PROTOCOLS = {
    'abaqus': ('model.inp', {}, ABAQUS_ARGS, ABAQUS_PRODUCTS),
    'comsol': ('model.mph', {'study': 'std1'}, COMSOL_ARGS, COMSOL_PRODUCTS),
    'fluent': ('model.cas.h5', {'iterations': 10}, FLUENT_ARGS, FLUENT_PRODUCTS),
    'starccm': ('model.sim', {}, STARCCM_ARGS, STARCCM_PRODUCTS),
    'hypermesh': ('model.hm', {}, HYPERMESH_ARGS, HYPERMESH_PRODUCTS),
}
LAUNCHER = '''
import os
from pathlib import Path
import sys
import time

legacy = MODEL == 'model.cas.h5' and Path('model.cas').is_file()
case_file = 'model.cas' if legacy else MODEL
deck = Path(case_file).read_bytes()
assert len(deck) > MIN_INPUT_SIZE
expected = ARGUMENTS.copy()
if MODEL == 'model.sim':
    macro = Path('SaveVibeFlowResult.java').read_text(encoding='ascii')
    assert macro.index('saveState(resolvePath("result.sim"))') < macro.index('resolvePath("save.complete")')
    assert Path('profile').is_dir()
    if b'mesh-and-solve' in deck:
        expected[5] = 'mesh,' + expected[5]
assert sys.argv[1:] == expected
if MODEL == 'model.cas.h5':
    journal = Path('solve.jou').read_text(encoding='ascii').splitlines()
    assert '/file/set-batch-options no yes yes no' in journal
    assert '/file/write-case-data result.cas.h5' in journal
    assert journal.index('/file/cff-files? yes') < journal.index('/file/write-case-data result.cas.h5')
    if Path('model.dat.h5').exists():
        assert b'restart-data' in Path('model.dat.h5').read_bytes()
        assert '/file/read-case-data model.cas.h5' in journal
        assert '/solve/dual-time-iterate 3 10' in journal
        assert all(not line.startswith('/solve/initialize/') for line in journal)
    elif legacy:
        assert b'restart-data' in Path('model.dat').read_bytes()
        assert journal.index('/file/cff-files? no') < journal.index('/file/read-case-data model.cas')
        assert '/solve/iterate 10' in journal
        assert all(not line.startswith('/solve/initialize/') for line in journal)
    else:
        assert '/file/read-case model.cas.h5' in journal
        assert '/solve/initialize/hyb-initialization' in journal
        assert '/solve/iterate 10' in journal
if b'missing-products' in deck:
    sys.exit(0)
if b'unconfirmed-save' in deck:
    Path('result.sim').write_bytes(PRODUCTS['result.sim'])
    sys.exit(0)
if MODEL == 'model.hm' and (b'test-publication' in deck or b'empty-mesh' in deck):
    from tkinter import Tcl

    abaqus_export = b'abaqus-template' in deck
    template_dir = Path.cwd() / 'test_templates'
    solver = 'abaqus' if abaqus_export else 'optistruct'
    template_path = template_dir / 'feoutput' / solver / ('standard.3d' if abaqus_export else solver)
    template_path.parent.mkdir(parents=True)
    template_path.write_text('Test-only template marker', encoding='ascii')
    output_name = 'result.inp' if abaqus_export else 'result.fem'

    def read_model(path):
        assert Path(path).name == 'model.hm' and Path(path).read_bytes() == deck
        return ''

    def export_deck(template, output, *options):
        assert Path(template) == template_path and Path(output).name == output_name
        assert options == ('0', '0', '1', '1', '0')
        Path(output).write_bytes(b'Test-only exported product')
        return ''

    tcl = Tcl()
    tcl.createcommand('hm_info', lambda *args: template_dir.as_posix())
    tcl.createcommand('*readfile', read_model)
    tcl.createcommand('*templatefileset', lambda path: '')
    tcl.createcommand('*createmark', lambda *args: '')
    tcl.createcommand('hm_marklength', lambda entity, mark: 4 if entity == 'nodes' else int(b'empty-mesh' not in deck))
    tcl.createcommand('*feoutputmergeincludefiles', lambda value: '')
    tcl.createcommand('*feoutputwithdata', export_deck)
    tcl.call('source', 'export.tcl')
    sys.exit(0)
if b'test-publication' in deck:
    for name, value in PRODUCTS.items():
        Path(name).write_bytes(value)
    if MODEL == 'model.sim':
        Path('save.complete').write_bytes(bytes([1]))
    sys.exit(0)
if b'cancel-test' not in deck:
    print('Intentional native protocol failure', flush=True)
    sys.exit(23)
Path(__file__).with_name('child.pid').write_text(str(os.getpid()), encoding='ascii')
time.sleep(90)
'''


def put(api, path, suffix):
    if path.endswith('.inp'):
        content = '*Heading\nVibeFlow protocol check\n*Step\n*Static\n*End Step\n' + suffix
        api.call('PUT', api.prefix + '/file', {'path': path, 'content': content})
    else:
        content = bytes(range(256)) * 8192 + suffix.encode('ascii')
        if path.endswith('.dat.h5'):
            content = bytes(512) + HDF5 + content
        elif path.endswith('.h5'):
            content = HDF5 + content
        response = httpx.post(api.base + '/api/v1' + api.prefix + '/upload',
            params={'path': path}, headers={'Authorization': 'Bearer ' + api.token},
            files={'file': (Path(path).name, content, 'application/octet-stream')}, trust_env=False, timeout=45)
        assert response.status_code == 200, response.status_code
        return hashlib.sha256(content).hexdigest()


def submit(api, backend, body, *, key=None, expect=200):
    operation = 'export' if backend == 'hypermesh' else 'solve'
    return api.call('POST', api.prefix + '/' + backend + ':' + operation, body,
                    expect=expect, extra_headers={'Idempotency-Key': key or uuid4().hex})


def published_receipts(api, backend):
    files = api.call('GET', api.prefix + '/tree')['entries']
    stage = 'B-mesh' if backend == 'hypermesh' else 'C-run'
    paths = (f['path'] for f in files if f['path'].startswith(f'{stage}/{backend}/')
             and f['path'].endswith('/analysis-receipt.json'))
    return [json.loads(api.call('GET', api.prefix + '/file?path=' + quote(path))['content']) for path in paths]


def check(store, backend):
    model, options, arguments, products = PROTOCOLS[backend]
    path = ('B-mesh/' if backend == 'hypermesh' else 'C-run/') + model
    body = {'input_path': path, **options}
    if backend != 'hypermesh':
        body['cpus'] = 1
    minimum_input_size = 0 if backend == 'abaqus' else 1024 * 1024
    variable = 'VIBEFLOW_' + backend.upper()
    os.environ.pop(variable, None)
    if store is not None:
        os.environ.update(store.settings())
    with IsolatedServer(backend + '-unavailable') as api:
        api.create_case('Unconfigured commercial solver')
        put(api, path, '')
        state = api.call('GET', api.prefix + '/' + backend + ':environment')
        assert not state['available'] and state['license'] == 'unverified'
        submit(api, backend, body, expect=503)
        assert not api.call('GET', api.prefix + '/jobs')

    api = IsolatedServer(backend + '-owned-protocol')
    program = api.root / 'protocol_launcher.py'
    command = api.root / 'Test launcher.cmd'
    program.write_text(f'MODEL = {model!r}\nARGUMENTS = {arguments!r}\nPRODUCTS = {products!r}\n'
                       f'MIN_INPUT_SIZE = {minimum_input_size}\n' + LAUNCHER, encoding='ascii')
    command.write_text(f'@echo off\n"{sys.executable}" -B "{program}" %*\nexit /b %errorlevel%\n',
                       encoding='utf8', newline='\r\n')
    os.environ[variable] = str(command)
    with api:
        api.create_case('Commercial solver native protocol')
        assert api.call('GET', api.prefix + '/' + backend + ':environment')['available']
        if backend == 'abaqus':
            put(api, path, '*Include,\n INPUT=other.inp\n')
            submit(api, backend, body, expect=400)
        elif backend == 'comsol':
            submit(api, backend, {**body, 'study': 'invalid study'}, expect=422)
            submit(api, backend, {**body, 'input_path': 'C-run/model.class'}, expect=400)
        elif backend == 'starccm':
            submit(api, backend, {**body, 'operation': 'custom_macro'}, expect=422)
            submit(api, backend, {**body, 'input_path': 'C-run/model.java'}, expect=400)
        elif backend == 'hypermesh':
            submit(api, backend, {**body, 'format': 'unknown'}, expect=422)
            submit(api, backend, {**body, 'input_path': 'B-mesh/export.tcl'}, expect=400)
        else:
            put(api, path, '')
            submit(api, backend, {**body, 'iterations': 0}, expect=422)
            submit(api, backend, {**body, 'input_path': 'C-run/model.jou'}, expect=400)
            submit(api, backend, {**body, 'data_path': 'C-run/legacy.dat'}, expect=400)
        assert not api.call('GET', api.prefix + '/jobs')

        put(api, path, '')
        key = uuid4().hex
        failed = submit(api, backend, body, key=key)['job_id']
        failure = api.wait_job(failed, 50, expected='failed')
        assert 'Intentional native protocol failure' in failure['log_backlog']
        assert submit(api, backend, body, key=key)['job_id'] == failed

        put(api, path, '** cancel-test\n')
        cancelled = submit(api, backend, body)['job_id']
        pid_file = api.root / 'child.pid'
        deadline = time.monotonic() + 40
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(.15)
        child = observe(int(pid_file.read_text(encoding='ascii')))
        api.call('POST', f'/jobs/{cancelled}:cancel', {})
        api.wait_job(cancelled, 50, expected='cancelled')
        assert probe(child)['state'] == 'exited'

        put(api, path, '** missing-products\n')
        missing = submit(api, backend, body)['job_id']
        api.wait_job(missing, 50, expected='failed')
        unconfirmed_save = None
        if backend == 'starccm':
            put(api, path, '** unconfirmed-save\n')
            unconfirmed_save = submit(api, backend, body)['job_id']
            rejected_save = api.wait_job(unconfirmed_save, 50, expected='failed')
            assert 'STAR-CCM+' in rejected_save['log_backlog']
        empty_mesh = None
        if backend == 'hypermesh':
            put(api, path, '** empty-mesh\n')
            empty_mesh = submit(api, backend, body)['job_id']
            rejected_mesh = api.wait_job(empty_mesh, 50, expected='failed')
            assert 'The model has no finite-element mesh' in rejected_mesh['log_backlog']

        input_digest = put(api, path, '** test-publication\n')
        published = submit(api, backend, body)['job_id']
        api.wait_job(published, 60)
        receipt, = published_receipts(api, backend)
        assert receipt['job_id'] == published and set(receipt['products']) == set(products)
        if backend == 'comsol':
            assert receipt['study'] == 'std1' and receipt['input'] == path
        export_run = None
        if backend == 'hypermesh':
            assert receipt['format'] == 'optistruct' and receipt['mesh'] == {'nodes': 4, 'elements': 1}
            converted = put(api, path, '** test-publication abaqus-template\n')
            export_run = submit(api, backend, {**body, 'format': 'abaqus'})['job_id']
            job = api.wait_job(export_run, 60)
            assert job['type'] == 'mesh'
            exported, = (r for r in published_receipts(api, backend) if r['job_id'] == export_run)
            assert exported['format'] == 'abaqus' and exported['input_sha256'] == converted
            assert exported['mesh'] == receipt['mesh'] and set(exported['products']) == {'result.inp', 'mesh-summary.json'}
        mesh_run = None
        if backend == 'starccm':
            assert receipt['operation'] == 'solve' and receipt['input_sha256'] == input_digest
            mesh_input = put(api, path, '** test-publication mesh-and-solve\n')
            mesh_run = submit(api, backend, {**body, 'operation': 'mesh_then_solve'})['job_id']
            api.wait_job(mesh_run, 60)
            meshed, = (r for r in published_receipts(api, backend) if r['job_id'] == mesh_run)
            assert meshed['operation'] == 'mesh_then_solve' and meshed['input_sha256'] == mesh_input
            assert set(meshed['products']) == {'result.sim'}
        paired_run = legacy_run = None
        if backend == 'fluent':
            assert receipt['initialization'] == 'hybrid' and receipt['input_sha256'] == input_digest
            data_path = 'C-run/cold-restart.dat.h5'
            data_digest = put(api, data_path, '** restart-data\n')
            continuation = {**body, 'data_path': data_path, 'time_steps': 3}
            paired_run = submit(api, backend, continuation)['job_id']
            api.wait_job(paired_run, 60)
            paired, = (r for r in published_receipts(api, backend) if r['job_id'] == paired_run)
            assert paired['time_steps'] == 3 and paired['initialization'] is None
            assert paired['additional_inputs'][data_path]['sha256'] == data_digest
            assert paired['input_sha256'] == input_digest
            legacy_case = 'C-run/legacy.cas'
            legacy_data = 'C-run/legacy.dat'
            legacy_case_digest = put(api, legacy_case, '** test-publication\n')
            legacy_data_digest = put(api, legacy_data, '** restart-data\n')
            legacy_run = submit(api, backend, {**body, 'input_path': legacy_case, 'data_path': legacy_data})['job_id']
            api.wait_job(legacy_run, 60)
            restarted, = (r for r in published_receipts(api, backend) if r['job_id'] == legacy_run)
            assert restarted['time_steps'] is None and restarted['initialization'] is None
            assert restarted['input_sha256'] == legacy_case_digest
            assert restarted['additional_inputs'][legacy_data]['sha256'] == legacy_data_digest
            assert set(restarted['products']) == set(products)

    result = {'passed': True, 'backend': backend, 'storage': 'authenticated S3' if store else 'filesystem',
              'unconfigured_status': 503, 'invalid_input_rejected': True,
              'failed_job': failed, 'idempotent_replay': True, 'cancelled_job': cancelled,
              'child_pid': child['pid'], 'child_exit_verified': True,
              'missing_products_job_failed': missing, 'non_numerical_publication_job': published,
              'paired_transient_protocol_job': paired_run, 'legacy_pair_protocol_job': legacy_run,
              'unconfirmed_save_job_failed': unconfirmed_save, 'mesh_then_solve_protocol_job': mesh_run,
              'empty_mesh_job_failed': empty_mesh, 'abaqus_deck_export_protocol_job': export_run,
              'over_one_mib_input': bool(minimum_input_size),
              'licensed_solver_runs': 0, 'model_calls': 0}
    output = api.root / 'checks.json'
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf8')
    print('COMMERCIAL_BATCH_PROTOCOL_OK', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=tuple(PROTOCOLS), required=True)
    parser.add_argument('--seaweed', type=Path, help='Run the checks with a disposable authenticated SeaweedFS')
    options = parser.parse_args()
    if os.name != 'nt':
        parser.error('The protocol launcher checks require Windows')
    store_context = nullcontext()
    if options.seaweed:
        from s3testsupport import ObjectStore
        store_context = ObjectStore(executable=options.seaweed, authenticated=True, volume_max=6)
    with store_context as object_store:
        check(object_store, options.backend)
