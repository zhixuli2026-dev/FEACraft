"""Native CadQuery geometry tolerance, viewer buffers and fallback through public APIs."""

from base64 import b64decode
import json
import math
from pathlib import Path
import sys

from testsupport import IsolatedServer, SERVER


SOURCE = '''import cadquery as cq

def build_geometry(ctx):
    hardware = cq.Assembly(name='hardware')
    hardware.add(cq.Solid.makeBox(2, 2, 2), name='anchor')
    hardware.add(cq.Solid.makeCylinder(.5, 2), name='pin', loc=cq.Location(cq.Vector(4, 0, 0)))
    drilled = cq.Workplane('XY').box(9, 8, 4).edges('|Z').fillet(1).faces('>Z').workplane().hole(2)
    return {'drilled': drilled, 'curved': cq.Workplane('XY').sphere(3),
            'sheet': cq.Face.makePlane(3, 4), 'hardware': hardware}
'''

DEPENDENCY_MISSING = "import sys\nsys.modules['ocp_tessellate'] = None\n"
CONVERSION_FAILURE = '''from ocp_tessellate import convert
def failed_conversion(*args, **kwargs):
    raise ValueError('deliberate isolated OCP conversion error')
convert.to_ocpgroup = failed_conversion
'''


def preview(api, tolerance, key):
    return api.call('POST', api.prefix + '/geometry:preview', {'tolerance': tolerance},
                    extra_headers={'Idempotency-Key': key})


def verify_scene(scene, tolerance):
    assert scene['tolerance'] == tolerance
    assert scene['ocp_status'] == {'status': 'ready'}, scene['ocp_status']
    objects = {entry['name']: entry for entry in scene['objects']}
    assert set(objects) == {'drilled', 'curved', 'sheet', 'hardware'}
    assert all(entry['vertices'] and entry['faces'] for entry in objects.values())
    drilled = objects['drilled']['bbox']
    assert math.isclose(drilled['max'][0] - drilled['min'][0], 9, abs_tol=tolerance), drilled
    assert math.isclose(objects['sheet']['bbox']['max'][2], objects['sheet']['bbox']['min'][2], abs_tol=1e-5)
    payload = scene['ocp']
    assert payload['type'] == 'data' and payload['count'] == 5
    data = payload['data']
    roots = {part['name']: part for part in data['shapes']['parts']}
    assert set(roots) == set(objects)
    assert {part['name'] for part in roots['hardware']['parts']} == {'anchor', 'pin'}
    bounds = data['shapes']['bb']
    assert bounds['xmin'] <= -4.49 and bounds['xmax'] >= 4.49
    for part in data['instances']:
        for column in ('vertices', 'triangles', 'normals', 'edges', 'face_types', 'triangles_per_face'):
            buffer = part[column]
            assert buffer['shape'] and buffer['dtype'] and 'codec' in buffer
            if math.prod(buffer['shape']):
                assert b64decode(buffer['buffer'], validate=True)
    return {'ordinary_faces': {name: len(item['faces']) for name, item in objects.items()},
            'ocp_triangle_indices': [instance['triangles']['shape'][0] for instance in data['instances']],
            'shape_count': payload['count'], 'publication': scene['publication']['id']}


def exercise(api, report):
    api.create_case('Native OCP preview types')
    scene_url = api.prefix + '/geometry/scene'
    validation_url = api.prefix + '/geometry/validation'
    old_dictionary = api.call('GET', api.prefix + '/caseDict')
    automatic = api.put('A-geo/geometry.py', SOURCE)['auto_preview_job']
    api.wait_job(automatic)
    default = verify_scene(api.call('GET', scene_url), 0.1)
    assert api.call('GET', validation_url)['tolerance'] == 0.1

    key = 'ocp-coarse-preview-20261119'
    coarse = preview(api, 0.4, key)
    api.wait_job(coarse['job_id'])
    configured = verify_scene(api.call('GET', scene_url), 0.4)
    assert api.call('GET', validation_url)['tolerance'] == 0.4
    assert configured['ordinary_faces']['curved'] < default['ordinary_faces']['curved']
    assert configured['ocp_triangle_indices'][1] < default['ocp_triangle_indices'][1]
    assert preview(api, 0.4, key)['job_id'] == coarse['job_id']
    api.call('POST', api.prefix + '/geometry:preview', {'tolerance': 0.2}, expect=409,
             extra_headers={'Idempotency-Key': key})
    for invalid in (0, -0.01, True, '0.1', None):
        api.call('POST', api.prefix + '/geometry:preview', {'tolerance': invalid}, expect=422)

    fallback_runs = {}
    for marker, reason in ((DEPENDENCY_MISSING, 'ocp_unavailable'), (CONVERSION_FAILURE, 'ocp_conversion_failed')):
        job_id = api.put('A-geo/geometry.py', marker + SOURCE)['auto_preview_job']
        job = api.wait_job(job_id)
        scene = api.call('GET', scene_url)
        assert scene['ocp_status'] == {'status': 'fallback', 'reason': reason}
        assert 'ocp' not in scene and 'fell back to the triangle mesh' in job['log_backlog']
        mesh = {item['name']: len(item['faces']) for item in scene['objects']}
        assert mesh == default['ordinary_faces']
        assert api.call('GET', validation_url)['summary']['valid']
        fallback_runs[reason] = job_id

    final = api.put('A-geo/geometry.py', SOURCE)['auto_preview_job']
    api.wait_job(final)
    assert verify_scene(api.call('GET', scene_url), 0.1)['shape_count'] == 5
    old = api.call('GET', scene_url + '?publication_id=' + configured['publication'])
    assert verify_scene(old, 0.4) == configured
    assert api.call('GET', api.prefix + '/caseDict') == old_dictionary
    report.update(default=default, configured=configured, failures=fallback_runs, recovery_job=final,
                  stable_idempotency=True, old_publication_readable=True)


def main():
    api = IsolatedServer('geometry-ocp', access_log=True)
    print('GEOMETRY_OCP_EVIDENCE', api.root, flush=True)
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        with api:
            exercise(api, report)
        assert api.proc.poll() is not None
        report['passed'] = True
    except Exception as error:
        report['failure_type'] = type(error).__name__
        report['failure'] = str(error)[:1000]
        raise
    finally:
        (api.root / 'geometry-ocp.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    main()
