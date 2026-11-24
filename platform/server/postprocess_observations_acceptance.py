"""Exercise official NOAA acquisition and sealed time series with a real postprocess job."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlencode

from postprocess_array_formula_acceptance import FOAM, POINTS, RUN, UNIT, config
from testsupport import IsolatedServer, SERVER


OBSERVATION = {'station': '9447130', 'begin': '2025-01-15', 'end': '2025-01-15'}
FORECAST = {**OBSERVATION, 'product': 'predictions', 'units': 'english'}
OBS_SHA = '697aa08a6661aa55b25e4018c3fabcb3549cfc3859c423bae38639e4290b3395'
FORECAST_SHA = 'c9b98db1c659569a1ec7ee7a012fc3b57b1396a05f1a5a84412a18191f9d59f3'
LOCAL = 'D-post/references/noaa-prediction.csv'
OFF_HOUR = 'D-post/references/noaa-prediction-wrong-hour.csv'
PRESSURE = 'D-post/references/archived-c05-pressure-point.csv'
PRESSURE_BYTES = ('clock,p\n2025-01-15T00:00:00.2+00:00,-0.020066744506221795\n'
                  '2025-01-15T00:00:00.5+00:00,-0.02006241106228211\n')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def independent(observations, predictions):
    def rows(data):
        values = csv.reader(io.StringIO(data.decode('utf-8')))
        next(values)
        return {record[0]: float(record[1]) for record in values}

    reference, actual = rows(observations), rows(predictions)
    assert reference.keys() == actual.keys() and len(reference) == 24
    errors = [actual[time] * 0.3048 - height for time, height in reference.items()]
    squared = sum(error ** 2 for error in errors)
    return {'rmse': math.sqrt(squared / len(errors)),
            'l2_relative': math.sqrt(squared / sum(value ** 2 for value in reference.values())),
            'max_absolute_error': max(abs(error) for error in errors)}


def options(observed, predicted, *, invalid=False):
    data = config()
    official = {'path': observed['path'], 'receipt': observed['receipt']}
    local = {'path': LOCAL, 'timeColumn': '"Date Time"', 'valueColumn': 'Prediction',
             'timeZone': 'UTC', 'unit': 'ft', 'datum': 'MLLW'}
    reference = {'source': '"NOAA NOS CO-OPS Seattle 9447130, obs and prediction"',
                 'kind': 'regression', 'unit': 'm', 'l2_max': 0.11, 'series': official}
    references = {'official': {**reference, 'metric': 'station_series',
        'candidateSeries': {'path': predicted['path'], 'receipt': predicted['receipt']}},
        'generic': {**reference, 'metric': 'local_series', 'candidateSeries': local}}
    references['archived_c05'] = {'metric': 'on_mesh.value', 'kind': 'regression', 'l2_max': 1e-10,
        'source': '"Archived D12 array-formulas y5nke08w, C05 pressure replay"', 'unit': json.dumps(UNIT),
        'timeOrigin': '"2025-01-15T00:00:00+00:00"', 'point': POINTS[0],
        'series': {'path': PRESSURE, 'timeColumn': 'clock', 'valueColumn': 'p', 'timeZone': 'offset',
                   'unit': json.dumps(UNIT)}}
    if invalid:
        references.update({
            'wrong_time': {**reference, 'metric': 'wrong_time', 'candidateSeries': {**local, 'path': OFF_HOUR}},
            'wrong_unit': {**reference, 'metric': 'wrong_unit', 'candidateSeries': {**local, 'unit': 'Pa'}},
            'wrong_datum': {**reference, 'metric': 'wrong_datum', 'candidateSeries': {**local, 'datum': 'MSL'}},
            'wrong_source': {**reference, 'metric': 'wrong_source', 'candidateSeries': local, 'source': '""'},
        })
    data['objectives'] = {'comparisonTargets': {'alignment_contract': {
        'metric': 'station_series', 'reference': 'official', 'type': 'requirement'}}}
    data['derivedQuantities']['referenceValues'] = references
    return data


def run(args):
    from app.services.casedict import set_block

    server = IsolatedServer('postprocess-observations-d12')
    evidence = server.root / 'official-observations.json'
    receipt = {'passed': False, 'model_calls': 0, 'solver_input_sha256': FOAM, 'runs': {}}
    print('POSTPROCESS_OBSERVATIONS_EVIDENCE', server.root, flush=True)
    try:
        with server as api:
            api.create_case('Seattle observations and prediction alignment')
            path = api.prefix + '/postprocess/observations:import'
            foreign = api.call('POST', '/auth/register', {'name': 'Foreign observation reader',
                'email': 'foreign-observations-d12@example.com', 'password': 'isolated-test-123'})
            other = {'Authorization': 'Bearer ' + foreign['token']}
            api.call('POST', path, OBSERVATION, expect=404, extra_headers=other)
            api.call('POST', path, {**OBSERVATION, 'url': 'https://example.com'}, expect=422)
            api.call('POST', path, {**OBSERVATION, 'station': '0000000'}, expect=502)

            def raw(file, **policy):
                return api.call('GET', api.prefix + '/raw?' + urlencode({'path': file}), raw=True, **policy)

            def collect(request, expected_sha, expected_unit):
                resource = api.call('POST', path, request)
                csv_bytes = raw(resource['path'])
                assert resource['sha256'] == digest(csv_bytes) == expected_sha and resource['unit'] == expected_unit
                assert resource['datum'] == 'MLLW' and resource['timeZone'] == 'UTC' and resource['records'] == 24
                assert resource['http_status'] == 200 and resource['replayed'] is False
                assert resource['first_utc'] == '2025-01-15T00:00:00+00:00'
                assert resource['last_utc'] == '2025-01-15T23:00:00+00:00'
                archived = json.loads(raw(resource['receipt']))
                assert archived == {key: value for key, value in resource.items() if key != 'replayed'}
                return resource, csv_bytes

            observed, observations = collect(OBSERVATION, OBS_SHA, 'm')
            predicted, predictions = collect(FORECAST, FORECAST_SHA, 'ft')
            assert observed['quality_flags'] == {'I': {'0': 24}, 'L': {'0': 24}}
            assert api.call('POST', path, OBSERVATION) == {**observed, 'replayed': True}
            raw(observed['path'], expect=404, extra_headers=other)
            receipt['downloads'] = {'measurements': observed, 'predictions': predicted, 'replayed': True,
                                    'foreign': [404, 404], 'bad_request': 422, 'upstream_empty': 502}
            expected = independent(observations, predictions)
            assert math.isclose(expected['rmse'], 0.22838451851285077, rel_tol=1e-11)
            receipt['independent'] = expected
            for relative, checksum in FOAM.items():
                data = (args.foam_case / relative).read_bytes()
                assert digest(data) == checksum
                assert api.put(RUN + '/' + relative, data.decode('utf-8'))['content_sha256'] == checksum
            assert api.put(LOCAL, predictions.decode('utf-8'))['content_sha256'] == FORECAST_SHA
            assert api.put(PRESSURE, PRESSURE_BYTES)['content_sha256'] == digest(PRESSURE_BYTES.encode())
            off_hour = predictions.replace(b'00:00', b'00:01', 1)
            assert off_hour != predictions
            api.put(OFF_HOUR, off_hour.decode('utf-8'))
            api.put('E-report/report.md', '# Station acquisition and alignment evidence\n')

            def extract(label, invalid=False):
                current = api.call('GET', api.prefix + '/caseDict')
                after = set_block(current['content'], 'postProcessing', options(observed, predicted, invalid=invalid))
                api.call('PUT', api.prefix + '/caseDict', {'content': after, 'expected_revision': current['dict_revision']})
                started = api.call('POST', api.prefix + '/postprocess:run', {'run_dir': RUN},
                                   extra_headers={'Idempotency-Key': 'd12-observations-' + label})
                job = api.wait_job(started['job_id'], timeout=100)
                stop = job['attempt']['process_stop']
                assert stop['verified'] and stop['scope_empty']
                result = api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'job_id': job['id']}))
                metrics = result['metrics']
                assert metrics['readiness']['can_claim_validation'] is False
                assert metrics['readiness']['report_ready'] is not invalid
                comparisons = {entry['metric']: entry for entry in metrics['comparisons']}
                for name in ('station_series', 'local_series'):
                    compared = comparisons[name]
                    assert compared['status'] == 'pass'
                    assert all(math.isclose(compared[key], value, rel_tol=1e-12) for key, value in expected.items())
                    assert compared['alignment']['samples'] == 24 and compared['alignment']['actual_scale'] == 0.3048
                    assert compared['alignment']['unit'] == 'm' and compared['alignment']['datum'] == 'MLLW'
                    assert compared['alignment']['observed']['sha256'] == OBS_SHA
                    assert compared['alignment']['actual']['sha256'] == FORECAST_SHA
                assert metrics['extractions']['comparisonTargets'][0]['status'] == 'pass'
                archived = comparisons['on_mesh.value']
                assert archived['status'] == 'pass' and archived['alignment']['samples'] == 2
                assert archived['alignment']['actual']['point'] == POINTS[0] and archived['alignment']['unit'] == UNIT
                assert archived['l2_absolute'] <= 1e-12
                if invalid:
                    for name, diagnostic in {'wrong_time': 'UTC 时刻不一致', 'wrong_unit': '单位不兼容',
                            'wrong_datum': '垂直基准不一致', 'wrong_source': '需要来源'}.items():
                        assert comparisons[name]['status'] == 'invalid_reference' and diagnostic in comparisons[name]['blocker']
                manifest = json.loads(raw(result['result_dir'] + '/artifact-manifest.json'))
                recorded = {record['path']: record['sha256'] for record in manifest['inputs']}
                for file in (observed['path'], observed['receipt'], predicted['path'], predicted['receipt'], LOCAL, PRESSURE):
                    assert recorded[file] == digest(raw(file))
                receipt['runs'][label] = {'publication_id': result['publication']['id'], 'comparisons': comparisons,
                    'readiness': metrics['readiness'], 'input_sha256': recorded, 'stopped': stop}
                return result

            initial = extract('initial')
            extract('invalid', invalid=True)
            gated = api.call('POST', api.prefix + '/report:build', {}, expect=409)
            assert 'readiness' in json.dumps(gated, ensure_ascii=False), gated
            receipt['readiness_gate_status'] = 409
            repaired = extract('repair')
            assert repaired['metrics']['comparisons'] == initial['metrics']['comparisons']
            modified = observations.replace(b'3.11', b'3.12', 1)
            assert modified != observations
            changed = api.put(observed['path'], modified.decode('utf-8'))
            assert changed['content_sha256'] == digest(modified)
            api.call('POST', path, OBSERVATION, expect=409)
            api.call('POST', api.prefix + '/report:build', {}, expect=409)
            historic = api.call('GET', api.prefix + '/postprocess/results?' +
                urlencode({'publication_id': initial['publication']['id']}))
            assert historic['metrics'] == initial['metrics']
            api.call('PUT', api.prefix + '/file', {'path': observed['path'], 'content': observations.decode('utf-8'),
                                                  'expected_sha256': digest(modified)})
            refreshed = extract('source-restored')
            assert refreshed['metrics']['comparisons'] == initial['metrics']['comparisons']
            receipt['source_mutation'] = {'failed_reimport': 409, 'stale_gate': 409, 'historic': True,
                                          'restored_sha256': digest(raw(observed['path']))}
        receipt['api_exit_code'] = server.proc.poll()
        assert receipt['api_exit_code'] is not None
        receipt['passed'] = True
    except BaseException as error:
        receipt['failure'] = {'type': type(error).__name__, 'message': str(error)[:1500]}
        raise
    finally:
        evidence.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('POSTPROCESS_OBSERVATIONS_PASS', evidence, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--foam-case', type=Path, required=True)
    run(parser.parse_args())
