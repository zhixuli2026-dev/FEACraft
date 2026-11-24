"""Verify postprocess array formulas with sealed, previously solved icoFoam fields."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlencode

from testsupport import IsolatedServer, SERVER


RUN = 'C-run/d12-array-cavity'
UNIT = 'm^2/s^2'
TIMES = [0.2, 0.5]
POINTS = [[0.025, 0.03, 0.005], [0.075, 0.03, 0.005]]
FOAM = {
    'system/controlDict':'c6128a998d4d3624e41d09966b53323a63522d063b0274d430aabb9e84c34820',
    'constant/polyMesh/boundary':'4892e1ec8d62569cf2a6b23ec59316a4e21375a50b092e56f541eafb737417f2',
    'constant/polyMesh/faces':'5fc1d0e3cd48773b7c6e8fa4af3980892fed61561fe6340201b3645d631ac617',
    'constant/polyMesh/neighbour':'e88ac5b4308a4f7417ea04254d183b2b03bcf312c5b09fc52f35fdce396cc53f',
    'constant/polyMesh/owner':'24a31f09634192eb136f96b3486f67083e372f5763faa99b9c0266fd521deabb',
    'constant/polyMesh/points':'88c0e985d240f6c9c1eb5321bb4b6e9aa3b4280d67bfb8c79b7775183902f5e6',
    '0.2/U':'b7ff9bfdef215d83f640d7bbbd6154e91a5bf281651647d77c93d162d6221111',
    '0.2/p':'67ad22a56355d1c7859f5d40b9a8b2ab77f36e582deb1f300331fcebb2acc144',
    '0.5/U':'911f6cc07009414ca861907514e8dc3c711f267b4e4d24cdf07a4fa516517ea7',
    '0.5/p':'b55d0a36f3e0fd97b7c9ef8883420433cc1c5adfe509b62ca5bae97d620f066f',
}


def config(failure=None):
    sampled = {'source':'pressure_probes', 'field':'p', 'component':0, 'unit':json.dumps(UNIT)}
    line = {'source':'pressure_line', 'field':'p', 'unit':json.dumps(UNIT if failure != 'unit' else 'Pa')}
    line_start = [POINTS[0][0], 0.035, POINTS[0][2]] if failure == 'points' else POINTS[0]
    return {
        'runDir':RUN, 'fields':{'p':{'unit':json.dumps(UNIT)}},
        'dataExtraction':{
            'pointProbes':{'pressure_probes':{'times':TIMES, 'fields':['p'],
                                             'points':[coordinate for point in POINTS for coordinate in point]}},
            'lineSamples':{'pressure_line':{'times':list(reversed(TIMES)) if failure == 'time' else TIMES,
                                            'fields':['p'], 'start':line_start, 'end':POINTS[1], 'samples':2}},
            'volumeAverages':{'mean':{'field':'p', 'time':0.2}},
        },
        'derivedQuantities':{
            'formulas':{
                'legacy_scalar':{'expression':'abs(x) * 2', 'inputs':{'x':'"mean.p@0.2"'}, 'unit':json.dumps(UNIT)},
                'on_mesh':{'expression':'2 * x + y + mean', 'unit':json.dumps(UNIT),
                           'inputs':{'x':sampled, 'y':line, 'mean':'"mean.p@0.2"'}},
                'chained':{'expression':'maximum - x', 'unit':json.dumps(UNIT),
                           'inputs':{'maximum':'on_mesh.value', 'x':sampled}},
            },
        },
    }


def assert_formula_outputs(result, probe_csv, line_csv):
    document = result['metrics']
    metrics = {metric['name']:metric for metric in document['metrics']}
    probes = {(float(row['time']), int(row['probe_index'])):float(row['value'])
              for row in csv.DictReader(io.StringIO(probe_csv))
              if row['operation'] == 'pressure_probes' and row['field'] == 'p' and row['component'] == '0'}
    line = {(float(row['time']), int(row['sample_index'])):float(row['value'])
            for row in csv.DictReader(io.StringIO(line_csv))
            if row['field'] == 'p' and row['component'] == '0'}
    assert len(probes) == len(line) == 4 and probes.keys() == line.keys()
    assert probes[(0.2, 0)] != probes[(0.5, 0)]
    mean = metrics['mean.p@0.2']['value']
    scalar = metrics['legacy_scalar.value']
    assert scalar['unit'] == UNIT and scalar['value'] == abs(mean) * 2 and 'sampling' not in scalar
    actual = metrics['on_mesh.value']
    chained = metrics['chained.value']
    for metric in (actual, chained):
        assert metric['unit'] == UNIT
        assert metric['sampling'] == {'layout':'time,point', 'times':TIMES, 'points':POINTS}
    for t_index, time in enumerate(TIMES):
        for point in (0, 1):
            probe = probes[(time, point)]
            sampled_line = line[(time, point)]
            assert math.isclose(probe, sampled_line, rel_tol=1e-12, abs_tol=1e-12)
            expected = 2 * probe + sampled_line + mean
            assert math.isclose(actual['value'][t_index][point], expected, rel_tol=1e-12, abs_tol=1e-12)
            assert math.isclose(chained['value'][t_index][point], expected - probe, rel_tol=1e-12, abs_tol=1e-12)
    assert document['readiness']['report_ready'] and not document['readiness']['can_claim_validation']
    return {'sampling':actual['sampling'], 'unit':actual['unit'], 'values':actual['value'], 'chained':chained['value']}


def verify(args):
    from app.services.casedict import set_block

    seed = args.foam_case.resolve(strict=True)
    server = IsolatedServer('postprocess-arrays-d12')
    root = server.root
    report = {'passed':False, 'model_calls':0, 'solver_input_sha256':FOAM, 'negative_cases':{}}
    evidence = root / 'array-formulas.json'
    print('POSTPROCESS_ARRAY_EVIDENCE', root, flush=True)
    try:
        with server as api:
            api.create_case('Native VTK array formulas')
            for relative, expected in FOAM.items():
                data = (seed / relative).read_bytes()
                assert hashlib.sha256(data).hexdigest() == expected, relative
                uploaded = api.put(RUN + '/' + relative, data.decode('utf8'))
                assert uploaded['content_sha256'] == expected, relative

            def write_options(options):
                original = api.call('GET', api.prefix + '/caseDict')
                content = set_block(original['content'], 'postProcessing', options)
                return api.call('PUT', api.prefix + '/caseDict',
                                {'content':content, 'expected_revision':original['dict_revision']})

            def launch(label, expected):
                started = api.call('POST', api.prefix + '/postprocess:run', {'run_dir':RUN},
                                   extra_headers={'Idempotency-Key':'d12-array-' + label})
                job = api.wait_job(started['job_id'], timeout=100, expected=expected)
                stop = job.get('attempt', {}).get('process_stop')
                assert stop and stop['verified'] and stop['scope_empty'], job
                return job

            def result_for(job):
                return api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'job_id':job['id']}))

            def raw(path):
                return api.call('GET', api.prefix + '/raw?' + urlencode({'path':path}), raw=True)

            write_options(config())
            first = launch('initial', 'succeeded')
            initial = result_for(first)
            folder = initial['result_dir']
            probe_csv = raw(folder + '/probes.csv').decode('utf8')
            line_csv = raw(folder + '/pressure_line.csv').decode('utf8')
            report['first'] = assert_formula_outputs(initial, probe_csv, line_csv)
            original_metrics = raw(folder + '/metrics.json')
            report['first']['publication_id'] = initial['publication']['id']
            report['first']['metrics_sha256'] = hashlib.sha256(original_metrics).hexdigest()

            other = api.call('POST', '/auth/register', {'name':'Other extractor', 'email':'other-array-d12@example.com',
                'password':'isolated-test-123'})
            foreign = {'Authorization':'Bearer ' + other['token']}
            api.call('POST', api.prefix + '/postprocess:run', {'run_dir':RUN}, expect=404, extra_headers=foreign)
            api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'publication_id':initial['publication']['id']}),
                     expect=404, extra_headers=foreign)
            report['foreign_rejections'] = [404,404]

            for label, message in (
                ('points','输入数组的时刻或采样坐标不一致'),
                ('time','输入数组的时刻或采样坐标不一致'),
                ('unit','公式采样单位不一致或未声明'),
            ):
                write_options(config(label))
                job = launch(label, 'failed')
                log = job.get('log_backlog', '')
                assert message in log, log[-3000:]
                publication = job['attempt'].get('outputs', {}).get('publication')
                assert publication is None or publication['status'] not in ('committed', 'superseded')
                api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'job_id':job['id']}), expect=(404,409))
                assert result_for(first)['metrics'] == initial['metrics'] and raw(folder + '/metrics.json') == original_metrics
                report['negative_cases'][label] = {'job_id':job['id'], 'diagnostic':message, 'old_evidence_readable':True}

            write_options(config())
            repaired = launch('repaired', 'succeeded')
            final = result_for(repaired)
            assert final['publication']['id'] != initial['publication']['id']
            report['recovered'] = assert_formula_outputs(final, probe_csv, line_csv)
            report['recovered']['publication_id'] = final['publication']['id']
            assert report['first']['values'] == report['recovered']['values']
        report['api_exit_code'] = server.proc.poll()
        assert report['api_exit_code'] is not None
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print('POSTPROCESS_ARRAY_PASS', evidence, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--foam-case', type=Path, required=True)
    verify(parser.parse_args())
