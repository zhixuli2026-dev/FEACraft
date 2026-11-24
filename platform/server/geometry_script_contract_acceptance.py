"""Exercise CadQuery script context and export guards through registered geometry jobs."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode

from app.services.casedict import set_block
from geometry_export_identity_acceptance import stl_width
from testsupport import IsolatedServer, SERVER


CONTEXT = '''from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import cadquery as cq
from local_contract import extent

assert __name__ == 'vibeflow_case_geometry'
assert str(CASE_ROOT) == VIBEFLOW_CASE_ROOT and CASE_ROOT == case_root == ctx.case_root
assert Path(__file__) == Path(VIBEFLOW_GEOMETRY_SCRIPT) == CASE_ROOT / 'A-geo/geometry.py'
assert A_GEO_DIR == ctx.a_geo_dir == Path(__file__).parent
assert ctx.case_dict_path == CASE_ROOT / '0-caseDict/caseDict'
assert float(ctx.params['width']) == 6 and float(ctx.domain['scale']) == 2
assert ctx.tolerance == 0.1

@dataclass
class Dimensions:
    width: float

def build_geometry(ctx):
    size = Dimensions(extent(float(ctx.params['width']), float(ctx.domain['scale'])))
    body = cq.Workplane('XY').box(size.width, 2, 3)
    ctx.log('context-mode=' + ctx.mode)
    debug('geometry', 'context ready')
    show_object(body, 'display', options={'color': [64, 128, 192], 'opacity': '0.7'})
    show(body, 'short-hex', color='#abc', opacity=0.5)
    ctx.show('positional', body, '#ff0000', 0.35)
    cq.exporters.export(body, str(ctx.artifact_path('A-geo/inline.step')))
    return {'body': body}

def export_geometry(ctx, objects):
    assert ctx.mode == 'export' and objects['body'].val().Volume() >= 70
    assert ctx.artifact_path('hook.txt') == A_GEO_DIR / 'hook.txt'
    ctx.artifact_path('A-geo/hook.txt').write_text('real-export', encoding='utf8')
'''

TOP_LEVEL = '''from cadquery import Workplane
solid = cq.Solid.makeBox(2, 3, 4)
native = cq.Solid.makeBox(3, 1, 1).wrapped
assembly = cq.Assembly()
assembly.add(cq.Solid.makeBox(2, 2, 2), name='left')
assembly.add(cq.Solid.makeBox(2, 2, 2), name='right', loc=cq.Location(cq.Vector(5, 0, 0)))
'''

FORMS = {
    'list from build': ('def build(ctx):\n    return [cq.Solid.makeBox(4, 1, 1)]\n', {'object_0': 4}),
    'tuple objects': ('objects = (cq.Solid.makeBox(5, 1, 1).wrapped,)\n', {'object_0': 5}),
    'single result': ('result = cq.Solid.makeBox(6, 1, 1)\n', {'result': 6}),
    'stage only': ('ctx.stage("stage", cq.Solid.makeBox(8, 1, 1))\n', {'stage': 8}),
    'top-level': (TOP_LEVEL, {'solid': 2, 'native': 3, 'assembly': 7}),
}


GUARD_SCRIPT = '''import cadquery as cq
from cadquery import exporters

export_shape = exporters.export
body = cq.Workplane('XY').box(7, 3, 2)

def checkpoint(stage, exporter):
    target = ctx.artifact_path(TAG + '-' + stage + '.step')
    before = target.read_bytes() if target.exists() else None
    exporter(body, str(target))
    after = target.read_bytes() if target.exists() else None
    if ctx.mode == 'preview':
        assert after == before, 'inline exporter wrote during preview at ' + stage
        if before is None:
            assert not target.exists()
    else:
        assert after.startswith(b'ISO-10303-21;') and len(after) > 1000
    ctx.log('export-guard ' + stage + ' ' + ctx.mode)

checkpoint('top', cq.exporters.export)

def build_geometry(ctx):
    checkpoint('build', export_shape)
    if FAIL_PREVIEW and ctx.mode == 'preview':
        raise ValueError('intentional guard build failure')
    return {'guard-solid': body}

def export_geometry(ctx, objects):
    assert ctx.mode == 'export'
    assert list(objects) == ['guard-solid'] and abs(objects['guard-solid'].val().Volume() - 42) < 1e-8
    ctx.log('export-guard hook entered')
    ctx.artifact_path(TAG + '-hook.txt').write_text('returned real CAD objects', encoding='utf8')
    if FAIL_HOOK:
        raise ValueError('intentional guard export hook failure')
'''


def preview(api, source, *, expected='succeeded'):
    job_id = api.put('A-geo/geometry.py', source)['auto_preview_job']
    assert job_id is not None
    return api.wait_job(job_id, expected=expected)


def assert_widths(scene, expected):
    objects = {item['name']: item for item in scene['objects']}
    assert set(objects) == set(expected), objects.keys()
    for name, width in expected.items():
        bounds = objects[name]['bbox']
        assert abs(bounds['max'][0] - bounds['min'][0] - width) < 1e-5, (name, bounds)
        assert objects[name]['vertices'] and objects[name]['faces'], name
    return objects


def exercise(api, report):
    api.create_case('Geometry runner context')
    path = api.prefix + '/caseDict'
    original = api.call('GET', path)
    dictionary = set_block(original['content'], 'geometry', {'parameters': {'width': 6}, 'domain': {'scale': 2}})
    api.call('PUT', path, {'content': dictionary, 'expected_revision': original['dict_revision']})
    api.put('A-geo/local_contract.py', 'def extent(width, scale):\n    return width * scale\n')

    job = preview(api, CONTEXT)
    assert 'context-mode=preview' in job['log_backlog'] and 'geometry context ready' in job['log_backlog']
    scene_url = api.prefix + '/geometry/scene'
    scene = api.call('GET', scene_url)
    assert scene['messages'] == ['context-mode=preview', 'geometry context ready']
    objects = assert_widths(scene, {name: 12 for name in ('body', 'display', 'short-hex', 'positional')})
    assert (objects['display']['color'], objects['display']['alpha']) == ('#4080c0', 0.7)
    assert (objects['short-hex']['color'], objects['short-hex']['alpha']) == ('#aabbcc', 0.5)
    assert (objects['positional']['color'], objects['positional']['alpha']) == ('#ff0000', 0.35)
    assert (objects['body']['color'], objects['body']['alpha']) == ('#4080c0', 0.7)
    file_url = api.prefix + '/file?path=A-geo/'
    for path in ('inline.step', 'hook.txt'):
        api.call('GET', file_url + path, expect=404)

    exported = api.call('POST', api.prefix + '/geometry:export', {})
    export_job = api.wait_job(exported['job_id'])
    assert 'context-mode=export' in export_job['log_backlog']
    assert api.call('GET', file_url + 'hook.txt')['content'] == 'real-export'
    assert api.call('GET', file_url + 'inline.step')['content'].startswith('ISO-10303-21;')
    assert exported['publication_id'] == api.call('GET', api.prefix + '/geometry/results')['publication']['id']
    context_id = exported['publication_id']

    forms = {}
    for label, (source, widths) in FORMS.items():
        run = preview(api, 'import cadquery as cq\n' + source)
        scene = api.call('GET', scene_url)
        assert_widths(scene, widths)
        forms[label] = {'job': run['id'], 'objects_and_widths': widths}

    before = scene['publication']['id']
    failed = preview(api, 'raise ValueError("intentional CAD script failure")\n', expected='failed')
    assert 'intentional CAD script failure' in failed['log_backlog']
    history = api.call('GET', scene_url + '?publication_id=' + before)
    assert history['publication']['id'] == before
    assert_widths(history, FORMS['top-level'][1])
    resumed = preview(api, CONTEXT)
    assert 'context-mode=preview' in resumed['log_backlog']
    assert api.call('GET', scene_url)['publication']['id'] != before
    assert len(api.call('GET', scene_url + '?publication_id=' + context_id)['objects']) == 4
    api.call('GET', scene_url, expect=401, extra_headers={'Authorization': ''})
    other = api.call('POST', '/auth/register', {'email': 'other-geometry@example.com',
        'name': 'Other geometry owner', 'password': 'isolated-geometry-test'})
    api.call('GET', scene_url + '?publication_id=' + before, expect=404,
             extra_headers={'Authorization': 'Bearer ' + other['token']})
    report.update(forms=forms, context_preview=job['id'], context_export=exported['job_id'],
                  failed_preview=failed['id'], recovered_preview=resumed['id'], context_publication=context_id,
                  prior_publication_readable=True, anonymous_status=401, other_owner_status=404)


def guard_source(tag, *, fail_preview=False, fail_hook=False):
    return f'TAG = {tag!r}\nFAIL_PREVIEW = {fail_preview!r}\nFAIL_HOOK = {fail_hook!r}\n' + GUARD_SCRIPT


def guard_job(job, mode):
    log = job['log_backlog']
    for stage in ('top', 'build'):
        assert log.count(f'export-guard {stage} {mode}') == 1, log[-2000:]
    assert log.count('export-guard hook entered') == (1 if mode == 'export' else 0), log[-2000:]
    stop = job['attempt']['process_stop']
    assert stop['verified'], stop
    return job['id']


def guard_files(api, tag, *, expect_export):
    report = {}
    for side, expected in (
        ('top.step', b'ISO-10303-21;'),
        ('build.step', b'ISO-10303-21;'),
        ('hook.txt', b'returned real CAD objects'),
    ):
        route = api.prefix + '/raw?' + urlencode({'path': f'A-geo/{tag}-{side}'})
        data = api.call('GET', route, expect=200 if expect_export else 404, raw=True)
        if expect_export:
            assert data.startswith(expected), data[:100]
            report[side] = hashlib.sha256(data).hexdigest()
    return report


def exported_bytes(api, publication):
    route = api.prefix + '/geometry/results?' + urlencode({'publication_id': publication})
    result = api.call('GET', route)
    assert result['publication']['id'] == publication
    records = {item['path']: item for item in result['files']}
    assert set(records) == {'A-geo/exports/guard-solid.step', 'A-geo/exports/guard-solid.stl'}
    result = {}
    for path, item in records.items():
        query = urlencode({'publication_id': publication, 'path': path})
        data = api.call('GET', api.prefix + '/geometry/artifact?' + query, raw=True)
        assert hashlib.sha256(data).hexdigest() == item['sha256'] and len(data) == item['size']
        if path.endswith('.step'):
            assert data.startswith(b'ISO-10303-21;') and data.rstrip().endswith(b'END-ISO-10303-21;')
        else:
            assert abs(stl_width(data) - 7) < 1e-5
        result[path] = item['sha256']
    return result


def guard_workflow(api, mode):
    revision = api.call('GET', api.prefix + '/caseDict')['dict_revision']
    body = {'target': 'geometry', 'expected_revision': revision, 'budget': {'timeout_seconds': 160}}
    if mode == 'export':
        body['parameters'] = {'geometry_mode': 'export'}
    run_id = api.call('POST', api.prefix + '/workflow-runs', body)['id']
    route = f'/workflow-runs/{run_id}'
    deadline = time.monotonic() + 175
    while time.monotonic() < deadline:
        run = api.call('GET', route)
        if run['status'] == 'awaiting_confirmation':
            break
        assert run['status'] not in {'succeeded', 'failed', 'cancelled', 'interrupted'}, run
        time.sleep(.2)
    else:
        raise TimeoutError(f'Geometry workflow did not reach B confirmation: {run_id}')
    first = next(step for step in run['steps'] if step['key'] == 'geometry')
    attempt, = first['attempts']
    job_id = guard_job(api.wait_job(attempt['job_id']), mode)
    publication = first['publication']['id']
    confirm = next(step for step in run['steps'] if step['key'] == 'confirm_B')
    api.call('POST', api.prefix + '/caseDict:advance', {'state': 'B', 'confirmed': True,
        'expected_revision': confirm['confirmation_revision'], 'workflow_step_id': confirm['id'],
        'expected_step_version': confirm['version']})
    finished = api.call('GET', route)
    assert finished['status'] == 'succeeded' and all(item['status'] == 'succeeded' for item in finished['steps'])
    return {'run': run_id, 'job': job_id, 'publication': publication}


def exercise_export_guards(api, report):
    api.create_case('Geometry inline export and hook isolation')
    scene_route = api.prefix + '/geometry/scene'
    export_route = api.prefix + '/geometry:export'
    checkpoints = report['entrypoints'] = {}
    auto = preview(api, guard_source('guard-first'))
    checkpoints['automatic_preview'] = guard_job(auto, 'preview')
    guard_files(api, 'guard-first', expect_export=False)
    manual = api.call('POST', api.prefix + '/geometry:preview', {'tolerance': .15})
    checkpoints['manual_preview'] = guard_job(api.wait_job(manual['job_id']), 'preview')
    scene = api.call('GET', scene_route)
    assert scene['publication']['id'] == manual['publication_id'] and scene['tolerance'] == .15
    assert_widths(scene, {'guard-solid': 7})
    guard_files(api, 'guard-first', expect_export=False)
    api.call('GET', api.prefix + '/file?path=A-geo/geometry_manifest.json', expect=404)

    first = api.call('POST', export_route, {})
    checkpoints['manual_export'] = guard_job(api.wait_job(first['job_id']), 'export')
    initial = first['publication_id']
    original_files = exported_bytes(api, initial)
    report['manual_export_custom_paths'] = guard_files(api, 'guard-first', expect_export=True)
    manifest = json.loads(api.call('GET', api.prefix + '/file?path=A-geo/geometry_manifest.json')['content'])
    assert manifest['artifacts'] == [{'name': 'guard-solid', 'step': 'guard-solid.step', 'stl': 'guard-solid.stl'}]

    pre_failure = preview(api, guard_source('guard-error', fail_hook=True))
    checkpoints['preview_with_throwing_export_hook'] = guard_job(pre_failure, 'preview')
    before_failure = api.call('GET', scene_route)['publication']['id']
    failed_export = api.call('POST', export_route, {})
    failure = api.wait_job(failed_export['job_id'], expected='failed')
    checkpoints['failed_export'] = guard_job(failure, 'export')
    assert 'intentional guard export hook failure' in failure['log_backlog']
    assert api.call('GET', scene_route)['publication']['id'] == before_failure
    guard_files(api, 'guard-error', expect_export=False)
    assert exported_bytes(api, initial) == original_files

    failed_preview = preview(api, guard_source('guard-build-error', fail_preview=True), expected='failed')
    checkpoints['failed_preview'] = guard_job(failed_preview, 'preview')
    assert 'intentional guard build failure' in failed_preview['log_backlog']
    assert api.call('GET', scene_route + '?' + urlencode({'publication_id': before_failure}))['publication']['id'] == before_failure
    guard_files(api, 'guard-build-error', expect_export=False)

    repaired = preview(api, guard_source('guard-repaired'))
    checkpoints['corrected_preview'] = guard_job(repaired, 'preview')
    guard_files(api, 'guard-repaired', expect_export=False)
    workflow = checkpoints['workflow_default_preview'] = guard_workflow(api, 'preview')
    assert api.call('GET', scene_route)['publication']['id'] == workflow['publication']
    assert not api.call('GET', api.prefix + '/geometry/results')['files']
    guard_files(api, 'guard-repaired', expect_export=False)
    assert exported_bytes(api, initial) == original_files

    foreign = api.call('POST', '/auth/register', {
        'email': 'other-geometry-guard@example.com', 'name': 'Outside geometry guard',
        'password': 'isolated-geometry-guard-test'})
    outside = {'Authorization': 'Bearer ' + foreign['token']}
    api.call('POST', api.prefix + '/geometry:preview', {}, expect=404, extra_headers=outside)
    api.call('POST', export_route, {}, expect=404, extra_headers=outside)
    api.call('GET', api.prefix + '/geometry/results?' + urlencode({'publication_id': initial}), expect=404, extra_headers=outside)
    report['foreign_access_statuses'] = [404, 404, 404]

    workspace = api.call('GET', '/workspaces')[0]['id']
    separate = api.call('POST', f'/workspaces/{workspace}/cases', {'name': 'Explicit workflow CAD export'})
    api.prefix = f"/cases/{separate['id']}"
    checkpoints['workflow_case_automatic_preview'] = guard_job(preview(api, guard_source('guard-workflow')), 'preview')
    guard_files(api, 'guard-workflow', expect_export=False)
    exported = checkpoints['workflow_explicit_export'] = guard_workflow(api, 'export')
    report['workflow_export_custom_paths'] = guard_files(api, 'guard-workflow', expect_export=True)
    report['workflow_export_hashes'] = exported_bytes(api, exported['publication'])
    report['original_export_hashes'] = original_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export-guards', action='store_true')
    args = parser.parse_args()
    api = IsolatedServer('geometry-script-contract', access_log=True)
    print('GEOMETRY_SCRIPT_CONTRACT_EVIDENCE', api.root, flush=True)
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        with api:
            (exercise_export_guards if args.export_guards else exercise)(api, report)
        assert api.proc.poll() is not None
        report['passed'] = True
    except Exception as error:
        report['failure'] = str(error)[:1000]
        raise
    finally:
        (api.root / 'geometry-script-contract.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    main()
