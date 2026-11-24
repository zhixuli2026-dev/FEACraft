"""Check scalar uncertainty propagation over immutable real icoFoam results."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.parse import urlencode

from postprocess_array_formula_acceptance import FOAM, RUN, UNIT, config
from testsupport import IsolatedServer, SERVER


def options(with_invalid=False):
    post = config()
    post['dataExtraction']['volumeAverages']['mean'] = {'field':'p', 'times':[0.2, 0.5]}
    formulas = post['derivedQuantities']['formulas']
    formulas['weighted_pressure'] = {'expression':'x + 2*y', 'unit':json.dumps(UNIT),
        'inputs':{'x':'"mean.p@0.2"', 'y':'"mean.p@0.5"'}}
    formulas['kink'] = {'expression':'abs(x - y)', 'unit':json.dumps(UNIT),
        'inputs':{'x':'"mean.p@0.2"', 'y':'"mean.p@0.2"'}}
    formulas['centered'] = {'expression':'x - x', 'unit':json.dumps(UNIT),
        'inputs':{'x':'"mean.p@0.2"'}}
    formulas['joint_kink'] = {'expression':'min(abs(x),abs(y))', 'unit':json.dumps(UNIT),
        'inputs':{'x':'centered.value', 'y':'centered.value'}}
    formulas['large_offset'] = {'expression':'1e20 + x', 'unit':json.dumps(UNIT),
        'inputs':{'x':'"mean.p@0.2"'}}
    inputs = {'x':{'unit':json.dumps(UNIT), 'standard':0.01},
              'y':{'unit':json.dumps(UNIT), 'standard':0.02}}
    common = {'metric':'weighted_pressure.value', 'coverage':2,
              'source':'"Independently specified numerical covariance test input"', 'inputs':inputs}

    def covariance(xy, yx, source):
        return {'source':json.dumps(source), 'rows':{'x':{'x':0.0001, 'y':xy},
                                                   'y':{'x':yx, 'y':0.0004}}}

    values = {
        'legacy':{'metric':'weighted_pressure.value', 'standard':0.125, 'coverage':2,
                  'source':'"Standalone numerical standard example"'},
        'independent':common,
        'explicit':{**common, 'covariance':covariance(0.0001, 0.0001, 'Declared regression example')},
        'large_offset':{**common, 'metric':'large_offset.value', 'inputs':{'x':inputs['x']}},
    }
    if with_invalid:
        values.update({
            'wrong_unit':{**common, 'inputs':{**inputs, 'x':{**inputs['x'], 'unit':'Pa'}}},
            'indefinite':{**common, 'covariance':covariance(0.001, 0.001, 'Deliberate invalid PSD example')},
            'asymmetric':{**common, 'covariance':covariance(0.0001, 0, 'Deliberate invalid symmetry example')},
            'missing_source':{**common, 'covariance':covariance(0.0001, 0.0001, '')},
            'nondifferentiable':{**common, 'metric':'kink.value'},
            'joint_nondifferentiable':{**common, 'metric':'joint_kink.value'},
        })
    post['derivedQuantities']['uncertainty'] = values
    return post


def check_values(result, *, invalid=False):
    document = result['metrics']
    metrics = {record['name']:record['value'] for record in document['metrics']}
    estimates = {record['id']:record for record in document['extractions']['uncertainty']}
    x, y = metrics['mean.p@0.2'], metrics['mean.p@0.5']
    assert x != y and metrics['weighted_pressure.value'] == x + 2*y
    legacy = estimates['legacy']
    assert legacy['method'] == 'expanded=standard*coverage' and legacy['expanded'] == 0.25 and legacy['unit'] == UNIT
    for name, dependent, variance in (('independent','independent',0.0017), ('explicit','declared',0.0021)):
        item = estimates[name]
        assert item['status'] == 'pass' and item['method'] == 'first_order_covariance' and item['unit'] == UNIT
        assert item['covariance']['dependence'] == dependent and item['covariance']['order'] == ['x','y']
        assert item['covariance']['source']
        assert item['inputs']['x']['unit'] == item['inputs']['y']['unit'] == UNIT
        assert math.isclose(item['inputs']['x']['sensitivity'], 1.0, abs_tol=1e-7)
        assert math.isclose(item['inputs']['y']['sensitivity'], 2.0, abs_tol=1e-7)
        assert math.isclose(item['standard'], math.sqrt(variance), rel_tol=1e-7)
        assert math.isclose(item['expanded'], 2*math.sqrt(variance), rel_tol=1e-7)
    assert estimates['independent']['covariance']['rows'][0][1] == 0
    assert estimates['explicit']['covariance']['rows'][0][1] == 0.0001
    offset = estimates['large_offset']
    assert metrics['large_offset.value'] == 1e20 and offset['status'] == 'pass'
    assert offset['inputs']['x']['sensitivity'] == 1 and math.isclose(offset['expanded'], 0.02, rel_tol=1e-12)
    assert metrics['joint_kink.value'] == 0 and metrics['centered.value'] == 0
    assert document['readiness']['report_ready'] is (not invalid)
    assert not document['readiness']['can_claim_validation']
    if invalid:
        for name, message in {
            'wrong_unit':'单位与实际指标不一致', 'indefinite':'正半定',
            'asymmetric':'必须对称', 'missing_source':'必须声明来源',
            'nondifferentiable':'无可确认的局部灵敏度',
            'joint_nondifferentiable':'无可确认的局部灵敏度',
        }.items():
            assert estimates[name]['status'] == 'invalid' and message in estimates[name]['blocker'], estimates[name]
    return {'inputs':{'x':x, 'y':y}, 'estimates':estimates, 'readiness':document['readiness']}


def verify(args):
    from app.services.casedict import set_block

    seed = args.foam_case.resolve(strict=True)
    server = IsolatedServer('postprocess-uncertainty-d12')
    root = server.root
    report = {'passed':False, 'model_calls':0, 'solver_input_sha256':FOAM, 'runs':{}}
    path = root / 'uncertainty-propagation.json'
    print('POSTPROCESS_UNCERTAINTY_EVIDENCE', root, flush=True)
    try:
        with server as api:
            api.create_case('Native scalar uncertainty propagation')
            for relative, expected in FOAM.items():
                data = (seed / relative).read_bytes()
                assert hashlib.sha256(data).hexdigest() == expected, relative
                assert api.put(RUN + '/' + relative, data.decode('utf8'))['content_sha256'] == expected

            def run(label):
                original = api.call('GET', api.prefix + '/caseDict')
                content = set_block(original['content'], 'postProcessing', options(label == 'invalid'))
                api.call('PUT', api.prefix + '/caseDict', {'content':content, 'expected_revision':original['dict_revision']})
                started = api.call('POST', api.prefix + '/postprocess:run', {'run_dir':RUN},
                                   extra_headers={'Idempotency-Key':'d12-uncertainty-' + label})
                job = api.wait_job(started['job_id'], timeout=100)
                stop = job['attempt']['process_stop']
                assert stop['verified'] and stop['scope_empty']
                result = api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'job_id':job['id']}))
                report['runs'][label] = check_values(result, invalid=label == 'invalid')
                report['runs'][label]['stop'] = stop
                report['runs'][label]['publication_id'] = result['publication']['id']
                return result

            initial = run('initial')
            invalid = run('invalid')
            recovered = run('recovered')
            original_id = initial['publication']['id']
            historic = api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'publication_id':original_id}))
            assert historic['metrics'] == initial['metrics']
            assert len({result['publication']['id'] for result in (initial,invalid,recovered)}) == 3
            assert report['runs']['initial']['estimates'] == report['runs']['recovered']['estimates']
            report['historic_publication_readable'] = True
        report['api_exit_code'] = server.proc.poll()
        assert report['api_exit_code'] is not None
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print('POSTPROCESS_UNCERTAINTY_PASS', path, flush=True)


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--foam-case', type=Path, required=True)
    verify(parser.parse_args())
