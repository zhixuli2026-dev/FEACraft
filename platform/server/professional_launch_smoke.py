"""Actual CalculiX/SU2 argv preservation and durable owned-process receipts.

Uses disposable API/storage only. Solver fixtures come from the existing golden
tests; reading the literal CalculiX deck does not execute that legacy script.
"""
import ast
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote

from testsupport import IsolatedServer, SERVER


def calculix_deck():
    tree = ast.parse((SERVER / 'calculix_smoke.py').read_text(encoding='utf-8-sig'))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'DECK' for target in node.targets))


def settled(api, job_id, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = api.call('GET', f'/jobs/{job_id}')
        # A cancellation acknowledgment can set ended_at before native draining.
        # These tests all launch a scope, so require its durable stop receipt.
        if (value['ended_at'] is not None and not value['completion_pending']
                and value.get('attempt', {}).get('process_stop') is not None):
            return value
        time.sleep(.1)
    api.call('POST', f'/jobs/{job_id}:cancel')
    raise TimeoutError(f'Professional job {job_id} did not settle')


def main():
    checks, receipts = [], []
    server = IsolatedServer('professional-launch')
    with server as api:
        for backend in ('calculix', 'su2'):
            for literal in (False, True):
                api.create_case(f'{backend} literal={literal}')
                stem = 'model %VIBEFLOW_CASE_ROOT% & literal' if literal else 'model'
                suffix = '.inp' if backend == 'calculix' else '.cfg'
                path = f'C-run/{backend}/{stem}{suffix}'
                if backend == 'calculix':
                    source = calculix_deck()
                    body = {'input_path': path}
                    output = path[:-4] + '.frd'
                else:
                    example = SERVER / 'examples/su2'
                    source = (example / 'inv_NACA0012.cfg').read_text().replace('ITER= 250', 'ITER= 3')
                    source = source.replace('MESH_FILENAME= mesh_NACA0012_inv.su2',
                                            'MESH_FILENAME= C-run/su2/input.su2')
                    api.put('C-run/su2/input.su2', (example / 'mesh_NACA0012_inv.su2').read_text())
                    body = {'config_path': path}
                    output = 'flow.vtu'
                api.put(path, source)
                before = hashlib.sha256(api.call('GET', api.prefix + '/raw?path=' + quote(path), raw=True)).hexdigest()
                started = api.call('POST', api.prefix + f'/{backend}:solve', body)
                row = settled(api, started['job_id'])
                receipts.append({'backend': backend, 'literal_filename': literal, 'job': row})
                (api.root / 'launch-receipts.json').write_text(json.dumps(receipts, indent=2), encoding='utf8')
                assert row['status'] == 'succeeded', row
                attempt = row['attempt']
                identity, stop = attempt['process_identity'], attempt['process_stop']
                assert identity['case_id'] == api.case_id and identity['job_id'] == row['id']
                assert identity['attempt_id'] == attempt['id'] and identity['runtime'] == 'windows'
                assert stop['launch_nonce'] == identity['launch_nonce'] and stop['verified']
                assert stop['scope_empty'] and stop['scope_observation']['complete']
                observation = api.call('GET', f"/jobs/{row['id']}/process")
                assert observation['state'] == 'exited' and observation['identity'] == identity, observation
                assert hashlib.sha256(api.call('GET', api.prefix + '/raw?path=' + quote(path), raw=True)).hexdigest() == before
                artifact = api.call('GET', api.prefix + '/raw?path=' + quote(output), raw=True)
                assert (b'DISP' in artifact if backend == 'calculix' else b'<VTKFile' in artifact)
                receipts[-1].update(observation=observation, input_sha256=before,
                                    output_sha256=hashlib.sha256(artifact).hexdigest())
                checks.append(f'{backend}_{"literal" if literal else "ordinary"}_input_actual_solve_owned_exit')
                print('PASS ' + checks[-1], flush=True)
        (api.root / 'launch-receipts.json').write_text(json.dumps(receipts, indent=2), encoding='utf8')
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(server.root),
              'actual_solvers': ['CalculiX', 'SU2'], 'isolated_database': True, 'isolated_storage': True,
              'fault_matrix_complete': False}
    (server.root / 'launch-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
