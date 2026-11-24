"""Exercise per-object CadQuery exports through the public job and download APIs."""

from hashlib import sha256
import json
from pathlib import Path
import re
import struct
import sys
from urllib.parse import urlencode
from uuid import uuid4

from testsupport import IsolatedServer, SERVER


SHAPES = {
    'body/left': (2, 'body_left'),
    'body?left': (3, 'body_left_3'),
    'body_left_2': (4, 'body_left_2'),
    'upper': (5, 'upper'),
    'Upper': (6, 'Upper_2'),
    'NUL': (7, 'NUL_'),
}


def script(*, fail=False):
    widths = {name: width for name, (width, _) in SHAPES.items()}
    return (
        'import cadquery as cq\n'
        f'WIDTHS = {widths!r}\n'
        'def build_geometry(ctx):\n'
        '    return {name: cq.Workplane("XY").box(width, 2, 1) for name, width in WIDTHS.items()}\n'
        'def export_geometry(ctx, objects):\n'
        '    assert list(objects) == list(WIDTHS)\n'
        + ('    raise ValueError("intentional export hook error")\n' if fail else
           '    ctx.artifact_path("export-hook.txt").write_text("exported", encoding="utf8")\n')
    )


def stl_width(data):
    if len(data) > 84 and len(data) == 84 + 50 * struct.unpack_from('<I', data, 80)[0]:
        vertices = [coordinates for triangle in range(84, len(data), 50)
                    for coordinates in struct.unpack_from('<9f', data, triangle + 12)[::3]]
    else:
        vertices = [float(value) for value in re.findall(rb'vertex\s+([-+0-9.eE]+)', data)]
    assert vertices, 'CadQuery STL contained no vertices'
    return max(vertices) - min(vertices)


def verify_downloads(api, publication_id):
    query = '?' + urlencode({'publication_id': publication_id})
    result = api.call('GET', api.prefix + '/geometry/results' + query)
    files = {record['path']: record for record in result['files']}
    assert len(files) == 2 * len(SHAPES)
    records = {}
    for name, (width, stem) in SHAPES.items():
        for extension in ('step', 'stl'):
            path = f'A-geo/exports/{stem}.{extension}'
            url = api.prefix + '/geometry/artifact' + query + '&' + urlencode({'path': path})
            data = api.call('GET', url, raw=True)
            assert sha256(data).hexdigest() == files[path]['sha256']
            if extension == 'step':
                assert data.startswith(b'ISO-10303-21;')
            else:
                assert abs(stl_width(data) - width) < 1e-5, name
            records[path] = files[path]['sha256']
    return records


def exercise(api, report):
    api.create_case('Per-object STEP and STL exports')
    first_preview = api.put('A-geo/geometry.py', script())['auto_preview_job']
    api.wait_job(first_preview)
    export = api.prefix + '/geometry:export'
    first = api.call('POST', export, {})['job_id']
    api.wait_job(first)
    result = api.call('GET', api.prefix + '/geometry/results')
    publication_id = result['publication']['id']
    manifest = api.call('GET', api.prefix + '/file?path=A-geo/geometry_manifest.json')
    manifest = json.loads(manifest['content'])
    assert {item['name']: (item['step'], item['stl']) for item in manifest['artifacts']} == {
        name: (stem + '.step', stem + '.stl') for name, (_, stem) in SHAPES.items()}
    original = verify_downloads(api, publication_id)

    api.call('GET', api.prefix + '/geometry/results', expect=401,
             extra_headers={'Authorization': ''})
    foreign = api.call('POST', '/auth/register', {
        'email': f'{uuid4().hex}@example.com', 'password': 'isolated-test-123', 'name': 'Other owner',
    })
    api.call('GET', api.prefix + '/geometry/results', expect=404,
             extra_headers={'Authorization': 'Bearer ' + foreign['token']})

    pre_failure = api.put('A-geo/geometry.py', script(fail=True))['auto_preview_job']
    api.wait_job(pre_failure)
    failed = api.call('POST', export, {})['job_id']
    failed_job = api.wait_job(failed, expected='failed')
    assert 'intentional export hook error' in failed_job['log_backlog']
    assert verify_downloads(api, publication_id) == original

    pre_retry = api.put('A-geo/geometry.py', script())['auto_preview_job']
    api.wait_job(pre_retry)
    retry = api.call('POST', export, {})['job_id']
    api.wait_job(retry)
    current = api.call('GET', api.prefix + '/geometry/results')['publication']['id']
    assert current != publication_id
    assert set(verify_downloads(api, current)) == set(original)
    assert verify_downloads(api, publication_id) == original
    report.update(jobs={'preview_before_export': first_preview, 'export': first,
                        'preview_before_failed_export': pre_failure, 'failed_export_hook': failed,
                        'preview_before_retry': pre_retry, 'retried_export': retry},
                  original_publication=publication_id, new_publication=current,
                  object_count=len(SHAPES), original_files=original,
                  anonymous_status=401, other_owner_status=404)


def main():
    api = IsolatedServer('geometry-export-identity', access_log=True)
    print('GEOMETRY_EXPORT_IDENTITY_EVIDENCE', api.root, flush=True)
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    try:
        with api:
            exercise(api, report)
        assert api.proc.poll() is not None
        report['passed'] = True
    except Exception as error:
        report['failure'] = str(error)[:1000]
        raise
    finally:
        (api.root / 'geometry-export-identity.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    main()
