"""Verify the public dictionary-to-CadQuery binding with real Windows geometry."""

import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlencode

from app.services.casedict import find_block_span, set_block
from testsupport import IsolatedServer, SERVER


PARAMETERS = {'width': 8, 'depth': 5, 'thickness': 4, 'label': 'parameter-solid'}
INITIAL_DOMAIN = {
    'scaleX': 1.5, 'scaleY': 2, 'scaleZ': .5,
    'offsetX': 4, 'offsetY': -3, 'offsetZ': 2,
}
UPDATED_DOMAIN = {
    'scaleX': 2, 'scaleY': 1, 'scaleZ': 1.5,
    'offsetX': -6, 'offsetY': 9, 'offsetZ': -1,
}
INITIAL_BOUNDS = ((-2, -8, 4), (10, 2, 6))
UPDATED_BOUNDS = ((-14, 6.5, -1), (2, 11.5, 5))

SCRIPT = '''import cadquery as cq

def build_geometry(ctx):
    geometry = ctx.data['geometry']
    assert ctx.params == geometry['parameters'] and ctx.domain == geometry['domain']
    assert ctx.data['caseMeta']['caseName'] == 'B23 native binding sample'
    label = ctx.params['label']
    reference_height = float(ctx.data['physicalProblem']['referenceHeight'])
    lengths = tuple(float(ctx.params[key]) * float(ctx.domain[scale])
        for key, scale in [('width', 'scaleX'), ('depth', 'scaleY'), ('thickness', 'scaleZ')])
    if min(lengths) <= 0:
        raise ValueError('geometry domain scales must be positive')
    offsets = [float(ctx.domain[key]) for key in ('offsetX', 'offsetY', 'offsetZ')]
    offsets[2] += reference_height
    ctx.log('parameter binding mode=' + ctx.mode)
    return {label: cq.Workplane('XY').box(*lengths).translate(tuple(offsets))}

def export_geometry(ctx, objects):
    assert ctx.mode == 'export'
    body = objects[ctx.data['geometry']['parameters']['label']].val()
    assert body.isValid() and len(body.Solids()) == 1
    ctx.log('parameter binding exported volume=' + str(body.Volume()))
'''


def conditions(dictionary):
    return {'expected_revision': dictionary['dict_revision'], 'expected_sha256': dictionary['content_sha256']}


def geometry(domain):
    return {'generator': 'cadquery', 'parameters': PARAMETERS, 'domain': domain}


def dictionary_snapshot(api):
    saved = api.call('GET', api.prefix + '/caseDict')
    assert saved['parse_status'] == 'ok' and saved['workflow_state'] == 'A'
    assert hashlib.sha256(saved['content'].encode('utf8')).hexdigest() == saved['content_sha256']
    return saved


def confirm_write(api, previous, receipt):
    updated = dictionary_snapshot(api)
    assert updated['dict_revision'] == previous['dict_revision'] + 1 == receipt['dict_revision']
    assert updated['content_sha256'] == receipt['content_sha256'] != previous['content_sha256']
    return updated


def write_domain(api, previous, domain):
    receipt = api.call('PUT', api.prefix + '/caseDict/block', {
        'block': 'geometry', 'entries': geometry(domain), **conditions(previous)})
    current = confirm_write(api, previous, receipt)
    before = previous['parsed']
    after = current['parsed']
    assert before['geometry']['parameters'] == after['geometry']['parameters']
    assert {key: value for key, value in before.items() if key != 'geometry'} == {
        key: value for key, value in after.items() if key != 'geometry'}
    assert after['geometry']['domain'] == {key: str(value) for key, value in domain.items()}
    left_start, left_end = find_block_span(previous['content'], 'geometry')
    right_start, right_end = find_block_span(current['content'], 'geometry')
    assert previous['content'][:left_start] == current['content'][:right_start]
    assert previous['content'][left_end:] == current['content'][right_end:]
    return current


def document(api, name, publication=None):
    query = '?' + urlencode({'publication_id': publication}) if publication else ''
    return api.call('GET', api.prefix + '/geometry/' + name + query)


def measure(api, expected_bounds, publication=None, *, stale=False):
    scene = document(api, 'scene', publication)
    validation = document(api, 'validation', publication)
    assert scene['publication']['id'] == validation['publication']['id']
    if publication:
        assert scene['publication']['id'] == publication
    assert scene['publication']['stale'] is stale and validation['publication']['stale'] is stale
    visible, = scene['objects']
    solid, = validation['objects']
    assert visible['name'] == solid['name'] == PARAMETERS['label']
    assert visible['vertices'] and visible['faces']
    for component, target in zip(('min', 'max'), expected_bounds):
        assert all(abs(actual - value) < 1e-6 for actual, value in zip(visible['bbox'][component], target))
    lengths = [maximum - minimum for minimum, maximum in zip(*expected_bounds)]
    assert abs(solid['volume'] - lengths[0] * lengths[1] * lengths[2]) < 1e-7
    assert solid['counts'] == {'solids': 1, 'shells': 1, 'faces': 6, 'edges': 12, 'vertices': 8}
    assert solid['valid'] and solid['watertight'] and solid['manifold']
    assert validation['source_sha256'] == hashlib.sha256(SCRIPT.encode('utf8')).hexdigest()
    return {'publication': scene['publication']['id'], 'bounds': visible['bbox'], 'volume': solid['volume'],
            'case_input_sha256': validation['case_input_sha256'], 'stale': stale}


def job(api, identifier, expected='succeeded', mode='preview'):
    state = api.wait_job(identifier, expected=expected)
    assert state['attempt']['process_stop']['verified']
    if expected == 'succeeded':
        assert f'parameter binding mode={mode}' in state['log_backlog']
    if mode == 'export':
        assert 'parameter binding exported volume=' in state['log_backlog']
    return state


def step_bytes(api, publication, *, headers=None, expect=200):
    target = 'A-geo/exports/parameter-solid.step'
    query = urlencode({'publication_id': publication, 'path': target})
    raw = api.call('GET', api.prefix + '/geometry/artifact?' + query, raw=True,
                   extra_headers=headers, expect=expect)
    if expect != 200:
        return raw
    files = document(api, 'results', publication)['files']
    assert {item['path'] for item in files} == {target, 'A-geo/exports/parameter-solid.stl'}
    record = next(item for item in files if item['path'] == target)
    assert hashlib.sha256(raw).hexdigest() == record['sha256'] and len(raw) == record['size']
    assert raw.startswith(b'ISO-10303-21;')
    return raw


def native_shape(api, filename, data, bounds, expected_volume):
    import cadquery as cq

    destination = api.root / filename
    destination.write_bytes(data)
    shape = cq.importers.importStep(str(destination)).val()
    assert shape.isValid() and len(shape.Solids()) == 1 and len(shape.Faces()) == 6
    assert abs(shape.Volume() - expected_volume) < 1e-7
    actual = shape.BoundingBox()
    coordinates = ((actual.xmin, actual.ymin, actual.zmin), (actual.xmax, actual.ymax, actual.zmax))
    for side, expected in zip(coordinates, bounds):
        assert all(abs(value - target) < 1e-6 for value, target in zip(side, expected))
    return {'bounds': coordinates, 'volume': shape.Volume(), 'faces': len(shape.Faces()),
            'sha256': hashlib.sha256(data).hexdigest()}


def exercise(api, report):
    api.create_case('B23 native geometry parameter binding')
    dictionary = dictionary_snapshot(api)
    content = set_block(dictionary['content'], 'caseMeta', {'caseName': 'B23 native binding sample'})
    content = set_block(content, 'physicalProblem', {'referenceHeight': 3})
    content = set_block(content, 'geometry', geometry(INITIAL_DOMAIN))
    receipt = api.call('PUT', api.prefix + '/caseDict', {'content': content, **conditions(dictionary)})
    initial = confirm_write(api, dictionary, receipt)
    assert initial['content'] == content
    report['conditional_write'] = conditions(initial)

    script = api.put('A-geo/geometry.py', SCRIPT)
    auto = script['auto_preview_job']
    job(api, auto)
    report['automatic_preview'] = {'job': auto, **measure(api, INITIAL_BOUNDS)}

    old = api.call('POST', api.prefix + '/geometry:export', {})
    job(api, old['job_id'], mode='export')
    old_id = old['publication_id']
    old_measurement = measure(api, INITIAL_BOUNDS, old_id)
    old_step = step_bytes(api, old_id)
    report['initial_export'] = {'job': old['job_id'], **old_measurement,
        'native_step': native_shape(api, 'initial.step', old_step, INITIAL_BOUNDS, 240)}

    updated = write_domain(api, initial, UPDATED_DOMAIN)
    report['domain_update'] = conditions(updated)
    conflict = api.call('PUT', api.prefix + '/caseDict/block', {
        'block': 'geometry', 'entries': geometry(INITIAL_DOMAIN), **conditions(initial)}, expect=409)
    assert conflict['detail']['code'] == 'revision_conflict'
    assert dictionary_snapshot(api) == updated
    assert measure(api, INITIAL_BOUNDS, old_id, stale=True)['case_input_sha256'] == old_measurement['case_input_sha256']
    assert step_bytes(api, old_id) == old_step
    api.advance('B', expect=409)

    manual = api.call('POST', api.prefix + '/geometry:preview', {})
    job(api, manual['job_id'])
    new_measurement = measure(api, UPDATED_BOUNDS, manual['publication_id'])
    assert new_measurement['case_input_sha256'] != old_measurement['case_input_sha256']
    report['manual_preview'] = {'job': manual['job_id'], **new_measurement}

    invalid = write_domain(api, updated, {**UPDATED_DOMAIN, 'scaleY': 0})
    failing = api.call('POST', api.prefix + '/geometry:preview', {})
    failed = job(api, failing['job_id'], expected='failed')
    assert 'geometry domain scales must be positive' in failed['log_backlog']
    assert measure(api, UPDATED_BOUNDS, stale=True)['publication'] == manual['publication_id']
    failed_archive = api.call('GET', api.prefix + '/geometry/publications')
    assert any(row['id'] == failing['publication_id'] and row['status'] == 'failed' for row in failed_archive)
    assert step_bytes(api, old_id) == old_step
    api.advance('B', expect=409)
    report['failed_preview'] = {'job': failing['job_id'], 'publication': failing['publication_id']}

    repaired = write_domain(api, invalid, UPDATED_DOMAIN)
    assert repaired['parsed'] == updated['parsed']
    recovered = api.call('POST', api.prefix + '/geometry:export', {})
    job(api, recovered['job_id'], mode='export')
    recovered_id = recovered['publication_id']
    assert recovered_id not in {old_id, manual['publication_id'], failing['publication_id']}
    recovered_measurement = measure(api, UPDATED_BOUNDS, recovered_id)
    assert recovered_measurement['case_input_sha256'] == new_measurement['case_input_sha256']
    recovered_step = step_bytes(api, recovered_id)
    report['recovered_export'] = {'job': recovered['job_id'], **recovered_measurement,
        'native_step': native_shape(api, 'recovered.step', recovered_step, UPDATED_BOUNDS, 480)}
    assert step_bytes(api, old_id) == old_step
    report['original_step_unchanged'] = True

    outsider = api.call('POST', '/auth/register', {'email': 'b23-outside@example.com',
        'name': 'Outside B23 owner', 'password': 'isolated-b23-test-123'})
    authorization = {'Authorization': 'Bearer ' + outsider['token']}
    api.call('GET', api.prefix + '/caseDict', expect=404, extra_headers=authorization)
    api.call('PUT', api.prefix + '/caseDict/block',
             {'block': 'geometry', 'entries': geometry(INITIAL_DOMAIN), **conditions(repaired)},
             expect=404, extra_headers=authorization)
    step_bytes(api, old_id, headers=authorization, expect=404)
    assert dictionary_snapshot(api) == repaired
    assert api.advance('B')['workflow_state'] == 'B'
    report.update(second_account_status=[404, 404, 404], stale_editor_status=409, phase_B_confirmed=True)


def main():
    api = IsolatedServer('geometry-parameter-injection', access_log=True)
    print('GEOMETRY_PARAMETER_INJECTION_EVIDENCE', api.root, flush=True)
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        with api:
            exercise(api, report)
        assert api.proc.poll() is not None
        report['api_exited'] = report['passed'] = True
    except Exception as error:
        report['failure'] = str(error)[:1500]
        raise
    finally:
        (api.root / 'geometry-parameter-injection.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    main()
