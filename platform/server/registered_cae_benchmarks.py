"""Original CAE numerical criteria through the actual registered report workflow."""
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys
import time
import traceback
from urllib.parse import quote
import uuid
import zipfile

from app.services.casedict import set_block
from cae_fixtures import cavity_2d, cavity_3d
from testsupport import IsolatedServer

ROOT = Path(__file__).resolve().parents[2]


def thermal_errors(work, run_dir, cells=80):
    """Read solver doubles and actual mesh vertices, avoiding VTK float32 loss."""
    mesh = work / run_dir / 'constant/polyMesh'

    def body(name):
        text = re.sub(r'/\*.*?\*/|//[^\n]*', '', (mesh / name).read_text(encoding='utf8'), flags=re.S)
        text = re.sub(r'FoamFile\s*\{[^}]*\}', '', text)
        match = re.fullmatch(r'\s*(\d+)\s*\((.*)\)\s*', text, re.S)
        assert match, name
        return int(match[1]), match[2]

    count, text = body('points')
    points = [tuple(map(float, row.split())) for row in re.findall(r'\(([^()]*)\)', text)]
    assert len(points) == count and all(len(p) == 3 for p in points)
    count, text = body('faces')
    faces = [tuple(map(int, row.split())) for row in re.findall(r'\d+\s*\(([^()]*)\)', text)]
    assert len(faces) == count and all(len(f) == 4 for f in faces)
    count, text = body('owner')
    owners = list(map(int, text.split()))
    assert len(owners) == count == len(faces)
    count, text = body('neighbour')
    neighbours = list(map(int, text.split()))
    assert len(neighbours) == count
    vertices = [set() for _ in range(cells)]
    for index, face in enumerate(faces):
        vertices[owners[index]].update(face)
        if index < len(neighbours):
            vertices[neighbours[index]].update(face)
    assert all(len(v) == 8 for v in vertices)
    centres = [sum(points[i][0] for i in v) / 8 for v in vertices]
    text = (work / run_dir / '1/T').read_text(encoding='utf8')
    match = re.search(r'internalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;', text, re.S)
    assert match and int(match[1]) == cells
    temperatures = list(map(float, match[2].split()))
    assert len(temperatures) == cells and all(math.isfinite(t) for t in temperatures)
    errors = [value - (300 + 100 * x) for value, x in zip(temperatures, centres)]
    return math.sqrt(sum(e * e for e in errors) / cells), max(map(abs, errors))


def run_benchmark(kind, *, restart_at_stage=None, api_factory=None, measure_completed=None):
    sys.path.insert(0, str(ROOT / 'platform/tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    from cae_benchmarks import thermal_files
    assert kind in {'cavity2d', 'cavity3d', 'conduction', 'legacy_conduction'}
    assert restart_at_stage in {None, 'D', 'E'}
    legacy = kind == 'legacy_conduction'
    thermal = kind in {'conduction', 'legacy_conduction'}
    run_dir = 'C-run/conduction' if thermal else 'C-run/case-3d' if kind == 'cavity3d' else 'C-run/case-1'
    solver = 'laplacianFoam' if thermal else 'icoFoam'
    end_time = 1 if thermal else .1 if kind == 'cavity3d' else .5
    cells = 320 if legacy else 80 if thermal else 27000 if kind == 'cavity3d' else 400
    lx, ly, lz = (1, 1, 1) if thermal else (.1, .1, .1 if kind == 'cavity3d' else .01)
    inputs = thermal_files() if thermal else cavity_3d() if kind == 'cavity3d' else cavity_2d()
    if thermal:
        # Same domain, boundary temperatures, total cells and analytic tolerance.
        # The old 20x2x2 anisotropy fails the unchanged native Mesh OK gate.
        name = run_dir + '/system/blockMeshDict'
        assert '(20 2 2)' in inputs[name]
        inputs[name] = inputs[name].replace('(20 2 2)', '(20 4 4)' if legacy else '(10 4 2)')
    api = (api_factory or IsolatedServer)('cae-registered-' + kind)
    report = {'passed': False, 'benchmark': kind, 'real_model_calls': 0, 'mock_llm_calls': 0,
              'actual_native_jobs': [], 'native_stops': {}, 'confirmations': [], 'solver': solver,
              'run_dir': run_dir, 'expected_cells': cells, 'original_numerical_thresholds_preserved': True}
    entered = False

    def save():
        (api.root / 'benchmark.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')

    def stopped(identifier, expected=None):
        if identifier not in report['actual_native_jobs']:
            report['actual_native_jobs'].append(identifier)
        until = time.monotonic() + 300
        while time.monotonic() < until:
            job = api.call('GET', '/jobs/' + str(identifier))
            attempt = job.get('attempt') or {}
            stop = attempt.get('process_stop') or {}
            if job['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'} and stop.get('verified'):
                report['native_stops'][str(identifier)] = stop
                if expected:
                    assert job['status'] == expected, (identifier, job['status'], job.get('log_backlog', '')[-1800:])
                return job
            time.sleep(.15)
        raise TimeoutError('Native stop not verified: ' + str(identifier))

    def raw(path):
        return api.call('GET', api.prefix + '/raw?path=' + quote(path, safe=''), raw=True)

    def admitted(method, path, body, **options):
        until = time.monotonic() + 30
        while True:
            result = api.call(method, path, body, expect=(200, 409), **options)
            # Successful workflow projections also have a string `detail`.
            # FastAPI admission errors have the envelope {"detail": ...} only.
            if not isinstance(result, dict) or set(result) != {'detail'}:
                return result
            assert isinstance(result['detail'], dict) and result['detail'].get('code') == 'storage_quota_busy', result
            if time.monotonic() >= until:
                raise TimeoutError('Explicitly rejected write stayed storage_quota_busy: ' + path)
            report['admission_busy_retries'] = report.get('admission_busy_retries', 0) + 1
            time.sleep(.2)

    def put_file(path, content):
        return admitted('PUT', api.prefix + '/file', {'path': path, 'content': content})

    try:
        api.__enter__()
        entered = True
        api.create_case('N7 original ' + kind)
        current = api.call('GET', api.prefix + '/caseDict')
        content = current['content']
        if not legacy:
            content = set_block(content, 'caseMeta', {
            'discipline': 'thermal' if thermal else 'fluid', 'analysisType': 'steady' if thermal else 'transient',
            'dimensions': '2D' if kind == 'cavity2d' else '3D', 'units': {'length': 'm', 'temperature': 'K'}})
            content = set_block(content, 'geometry', {'generator': 'cadquery', 'parameters': {'lx': lx, 'ly': ly, 'lz': lz}})
        content = set_block(content, 'mesh', {'source': 'blockMesh', 'qualityTargets':
            ({'maxNonOrtho': 70, 'policy': 'fail'} if legacy else
             {'policy': 'fail', 'maxNonOrtho': 70, 'maxSkewness': 4, 'maxAspectRatio': 100})})
        content = set_block(content, 'numericalMethod', {'solver': solver})
        field = 'T' if thermal else 'p'
        post = {'runDir': run_dir, 'fields': {field: {'unit': 'K' if thermal else '"m^2/s^2"'}},
                'dataExtraction': {'volumeAverages': {'mean': {'field': field, 'time': end_time}},
                'pointProbes': {'center': {'fields': [field], 'points': [lx / 2, ly / 2, lz / 2]}}}}
        if thermal:
            post['derivedQuantities'] = {'referenceValues': {'linear_solution': {
                'metric': '"mean.T@1"', 'value': 350, 'unit': 'K', 'kind': 'verification',
                'source': '"analytic T(x)=300+100*x; insulated transverse faces"', 'l2_max': 1e-6}}}
        if legacy:
            # Preserve the original s3_core_workflow_smoke 320-cell slow case:
            # geometry export, 19-point line extraction and a PNG plot.
            post = {'runDir':run_dir,'fields':{'T':{'unit':'K'}},'dataExtraction':{
                'volumeAverages':{'mean':{'field':'T','time':1}},
                'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'nPoints':19,'times':[1]}}},
                'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
                    'unit':'K','kind':'verification','source':'"analytic linear conduction"','l2_max':1e-6}}},
                'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,'formats':['png'],'title':'"Temperature"'}}}}
        content = set_block(content, 'postProcessing', post)
        admitted('PUT', api.prefix + '/caseDict', {'content': content, 'expected_revision': current['dict_revision']})
        for name, data in inputs.items():
            put_file(name, data)
        geometry_script = "import cadquery as cq\ndef build_geometry(ctx):\n p=ctx.data['geometry']['parameters']\n return cq.Workplane('XY').box(float(p['lx']),float(p['ly']),float(p['lz']),centered=(False,False,False))\n"
        if legacy:
            geometry_script = ('import cadquery as cq\ndef build_geometry(ctx):\n'
                '    return {"box":cq.Workplane("XY").box(2,2,3)}\n'
                'def export_geometry(ctx, objects):\n'
                '    (ctx.a_geo_dir / "export-hook.txt").write_text("called")\n'
                '    cq.exporters.export(objects["box"], str(ctx.a_geo_dir / "inline-export.step"))\n')
        preview = put_file('A-geo/geometry.py', geometry_script)
        if preview.get('auto_preview_job'):
            stopped(preview['auto_preview_job'], 'succeeded')
        flow = admitted('POST', api.prefix + '/workflow-runs', {
            'target': 'report', 'run_dir': run_dir,
            **({'parameters': {'geometry_mode':'export','report_mode':'compose'}} if legacy else {}),
            'expected_revision': api.call('GET', api.prefix + '/caseDict')['dict_revision'],
            'budget': {'timeout_seconds': 300, 'max_log_bytes': 16777216}},
            extra_headers={'Idempotency-Key': str(uuid.uuid4())})
        report['workflow_id'] = flow['id']
        until = time.monotonic() + 900
        while time.monotonic() < until:
            flow = api.call('GET', '/workflow-runs/' + flow['id'])
            for step in flow['steps']:
                for attempt in step['attempts']:
                    if attempt['job_id'] not in report['actual_native_jobs']:
                        report['actual_native_jobs'].append(attempt['job_id'])
                if step['status'] == 'awaiting_confirmation':
                    for producer in flow['steps']:
                        if producer['status'] == 'succeeded':
                            for attempt in producer['attempts']:
                                stopped(attempt['job_id'], 'succeeded')
                    if step['confirmation_state'] == restart_at_stage and not report.get('API_restart'):
                        from cae_restart_acceptance import restart_at_confirmation
                        restart_at_confirmation(api, flow, step, report)
                    admitted('POST', api.prefix + '/caseDict:advance', {
                        'state': step['confirmation_state'], 'confirmed': True,
                        'expected_revision': step['confirmation_revision'],
                        'workflow_step_id': step['id'], 'expected_step_version': step['version']})
                    report['confirmations'].append(step['confirmation_state'])
                    save()
                    break
            if flow['status'] == 'succeeded':
                break
            assert flow['status'] not in {'failed', 'cancelled', 'interrupted'}, flow
            time.sleep(.2)
        else:
            raise TimeoutError('Registered geometry-to-report workflow did not complete')
        assert report['confirmations'] == ['B', 'C', 'D', 'E']
        jobs = {}
        for step in flow['steps']:
            if step['attempts']:
                jobs[step['key']] = stopped(step['attempts'][-1]['job_id'], 'succeeded')
        assert set(jobs) == {'geometry', 'mesh', 'solve', 'postprocess', 'report'}
        publications = {key: value['attempt']['outputs']['publication']['id'] for key, value in jobs.items()}
        for child, parent in [('mesh', 'geometry'), ('solve', 'mesh'), ('postprocess', 'solve'), ('report', 'postprocess')]:
            selected = jobs[child]['attempt']['workspace']['publications']
            assert publications[parent] in {row['publication_id'] for row in selected}, (child, parent, selected)
        report['lineage'] = {key: {'job_id': value['id'], 'attempt_id': value['attempt']['id'],
                                  'publication_id': publications[key]} for key, value in jobs.items()}
        report['input_sha256'] = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in inputs.items()}
        geometry = json.loads(raw('A-geo/validation.json'))
        assert geometry['summary']['valid'] and geometry['summary']['object_count'] == 1
        assert math.isclose(geometry['objects'][0]['volume'], 12 if legacy else lx * ly * lz, rel_tol=1e-9)
        quality = json.loads(raw('B-mesh/quality.json'))
        assert quality['mesh_ok'] and quality['status'] == 'pass'
        result = api.call('GET', api.prefix + '/solve/results')
        evidence = result['evidence']
        parsed = evidence['parsed_log']
        assert parsed['summary']['finished_cleanly'] and parsed['summary']['fatal_errors'] == 0
        assert math.isclose(parsed['summary']['last_time'], end_time, abs_tol=1e-12)
        assert evidence['inputs'] and evidence['outputs'] and not result['publication']['stale']
        numeric = {'last_time': parsed['summary']['last_time'], 'cells': cells}
        if not thermal:
            written = [r['path'] for r in evidence['outputs'] if r['path'].endswith('/U')]
            assert len(written) == 5, written
            residuals = parsed['residuals']
            if kind == 'cavity2d':
                assert parsed['summary']['time_points'] == 100
                velocity = residuals.get('Ux') or residuals.get('U')
                assert velocity and velocity[-1] < velocity[0]
                numeric['Ux_residual_drop_ratio'] = velocity[0] / velocity[-1]
            else:
                assert residuals.get('Uz') and any(v > 0 for v in residuals['Uz'])
                pressure = residuals['p']
                assert pressure[-1] < pressure[0] / 100
                numeric['p_residual_drop_ratio'] = pressure[0] / pressure[-1]
            numeric['written_velocity_times'] = written
        # Independent field checks use the actual cell centres, not assumed file ordering.
        import numpy as np
        import vtk
        from vtk.util.numpy_support import vtk_to_numpy
        case_root = next((api.root / 'files/workspaces').glob('*/cases/' + str(api.case_id)))
        work = case_root / jobs['solve']['attempt']['workspace']['run_path']
        marker = work / run_dir / (Path(run_dir).name + '.foam')
        assert marker.is_file(), marker
        reader = vtk.vtkOpenFOAMReader()
        reader.SetFileName(str(marker))
        reader.CreateCellToPointOff()
        reader.UpdateInformation()
        reader.EnableAllCellArrays()
        reader.UpdateTimeStep(end_time)
        grid = reader.GetOutput().GetBlock(0)
        assert grid.GetNumberOfCells() == cells
        values = vtk_to_numpy(grid.GetCellData().GetArray(field)).copy()
        assert np.isfinite(values).all()
        if thermal:
            l2, maximum = thermal_errors(work, run_dir, cells)
            assert l2 < 1e-6 and maximum < 1e-6, (l2, maximum)
            numeric.update(analytic_temperature_l2_K=l2, analytic_temperature_max_error_K=maximum,
                           thermal_grid=[20, 4, 4] if legacy else [10, 4, 2], analytic_coordinates='actual ASCII mesh cell centres; solver double text')
        extracted = api.call('GET', api.prefix + '/postprocess/results')
        assert extracted['metrics']['readiness']['report_ready'] and extracted['manifest']['run_inventory']
        if thermal:
            assert extracted['metrics']['readiness']['can_claim_verification']
        assert not extracted['metrics']['readiness']['can_claim_validation']
        document = api.call('GET', api.prefix + '/report/results')
        assert document['publication']['id'] == publications['report'] and not document['publication']['stale']
        downloads = {}
        for path, expected in document['manifest']['artifacts'].items():
            data = api.call('GET', api.prefix + '/report/artifact?publication_id=' + publications['report'] + '&path=' + quote(path, safe=''), raw=True)
            assert hashlib.sha256(data).hexdigest() == expected['sha256']
            if path.endswith('.html'):
                assert b'<html' in data.lower()
            elif path.endswith('.docx'):
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    assert b'<w:document' in archive.read('word/document.xml')
            (api.root / ('downloaded-' + Path(path).name)).write_bytes(data)
            downloads[path] = {'sha256': expected['sha256'], 'bytes': len(data)}
        assert any(p.endswith('.docx') for p in downloads) and any(p.endswith('.html') for p in downloads)
        report.update(numerical=numeric, downloads=downloads)
        save()
        if measure_completed is not None:
            measure_completed(api, flow, report)
        # A changed original field invalidates new engineering report admission.
        source_field = next(row['path'] for row in evidence['outputs'] if row['path'].endswith('/' + field))
        previous = raw(source_field)
        try:
            put_file(source_field, previous.decode() + '\n// N7 changed after evidence generation\n')
            api.call('POST', api.prefix + '/report:compose', {'overwrite': True}, expect=409)
            api.call('POST', api.prefix + '/report:build', expect=409)
        finally:
            # A remote write can leave the account settlement temporarily busy.
            # Retry only the explicit admission rejection; unknown writes and
            # unrelated conflicts are never replayed automatically.
            before_busy = report.get('admission_busy_retries', 0)
            put_file(source_field, previous.decode())
            report['source_restore_busy_retries'] = report.get('admission_busy_retries', 0) - before_busy
        # Restoring bytes is another write, not permission to forget the input
        # version change. Keep the fixed historical report available and stale.
        assert api.call('GET', api.prefix + '/report/results')['publication']['stale'] is True
        for path, expected in downloads.items():
            data = api.call('GET', api.prefix + '/report/artifact?publication_id=' + publications['report'] + '&path=' + quote(path, safe=''), raw=True)
            assert hashlib.sha256(data).hexdigest() == expected['sha256']
        report.update(functional_passed=True, numerical=numeric, downloads=downloads,
                      original_changed_field_rejected=True, same_workflow_source_to_report=True,
                      restored_bytes_do_not_erase_changed_input_version=True, pinned_report_bytes_preserved=True)
        (api.root / 'workflow-final.json').write_text(json.dumps(flow, indent=2) + '\n', encoding='utf8')
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:2200],
                            'frames': [{'file': Path(f.filename).name, 'line': f.lineno} for f in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                if hasattr(api, 'case_id'):
                    for flow in api.call('GET', api.prefix + '/workflow-runs'):
                        if flow['status'] not in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
                            api.call('POST', '/workflow-runs/' + flow['id'] + ':cancel', {'expected_version': flow['version']})
                    for job in api.call('GET', api.prefix + '/jobs'):
                        if job['status'] in {'queued', 'running'}:
                            api.call('POST', '/jobs/' + str(job['id']) + ':cancel')
                        stopped(job['id'])
                report['all_actual_native_stops_verified'] = True
            except BaseException as error:
                report['cleanup_failure'] = type(error).__name__ + ': ' + str(error)[:700]
            api.__exit__(None if report.get('functional_passed') else RuntimeError, None, None)
            report['API_exit_verified'] = api.proc.poll() is not None
        report['passed'] = bool(report.get('functional_passed') and report.get('all_actual_native_stops_verified') and report.get('API_exit_verified') and not report.get('cleanup_failure'))
        save()
        print('REGISTERED_CAE_RESULT ' + str(api.root / 'benchmark.json'), flush=True)
    if not report['passed']:
        raise AssertionError('Actual benchmark failed; inspect ' + str(api.root / 'benchmark.json'))
    return report
