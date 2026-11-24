"""Actual CAD/VTK conversions, competing writers, deadlines and DeepSeek readback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time
from urllib.parse import urlencode

from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put


STEP = 'A-geo/box.step'
MESH = 'A-geo/tetra.msh'
REPAIR = 'A-geo/exports/repaired-scale.step'
VALIDATION = 'A-geo/exports/repaired-scale-validation.json'

FIXTURE = '''
from contextlib import asynccontextmanager, contextmanager
import traceback
from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from app.config import get_settings
from app.routers import geometry
from app.services import geometry_conversion, request_deadline

get_settings().storage_maintenance_enabled = False
prepare = geometry_conversion._prepare
reserve = geometry_conversion.reserve
read_deadline = geometry.file_read_deadline

@app.exception_handler(HTTPException)
async def trace_quota(request, error):
    if isinstance(error.detail, dict) and error.detail.get('code') == 'storage_quota_busy':
        frames = [[str(frame.filename), frame.lineno, frame.name] for frame in traceback.extract_tb(error.__traceback__)[-12:]]
        with (root / 'geometry-quota-trace.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps(frames) + '\\n')
    return await http_exception_handler(request, error)

def pause_conversion(reader, path, convert, produces_file):
    kind = 'mesh' if path.endswith('.msh') else 'repair' if produces_file else 'import'
    def observed(source):
        result = convert(source)
        if (root / ('hold-' + kind)).exists():
            (root / ('entered-' + kind)).touch()
            deadline = time.monotonic() + 25
            while not (root / ('release-' + kind)).exists():
                if time.monotonic() >= deadline:
                    raise ValueError('The conversion check did not release the worker')
                time.sleep(.02)
        return result
    return prepare(reader, path, observed, produces_file)

@asynccontextmanager
async def targeted_deadline(code, label):
    async with read_deadline(code, label):
        if (root / 'short-conversion').exists():
            async with request_deadline.scope(4):
                yield
        else:
            yield

@contextmanager
def constrained_reserve(size):
    settings = get_settings()
    previous = settings.node_storage_limit_mb
    try:
        if (root / 'limited-geometry-node').exists():
            settings.node_storage_limit_mb = 16
        with reserve(size):
            yield
    finally:
        settings.node_storage_limit_mb = previous

import time
geometry_conversion._prepare = pause_conversion
geometry_conversion.reserve = constrained_reserve
geometry.file_read_deadline = targeted_deadline
'''


def scenario(api, **_):
    import cadquery as cq
    import meshio

    def make_step(name, sides):
        target = api.root / name
        cq.exporters.export(cq.Workplane('XY').box(*sides), str(target))
        return target.read_text(encoding='utf8')

    original = make_step('original.step', (1, 2, 3))
    changed = make_step('changed.step', (2, 3, 4))
    tetra = api.root / 'tetra.msh'
    meshio.write_points_cells(tetra, [[0,0,0],[1,0,0],[0,1,0],[0,0,1]],
                             [('tetra', [[0,1,2,3]])], file_format='gmsh22', binary=False)
    mesh_text = tetra.read_text(encoding='utf8')
    put(api, STEP, original)
    put(api, MESH, mesh_text)
    prefix = api.prefix + '/geometry'
    repair = prefix + '/repair'
    mesh = prefix + '/msh-preview'
    imported = prefix + '/import-scene?' + urlencode({'path':STEP})
    spec = {'path':STEP,'operation':'scale','factor':2}

    def raw(path, status=200):
        return api.call('GET', api.prefix+'/raw?'+urlencode({'path':path}), expect=status, raw=True)

    def signal(kind):
        return api.root / ('entered-' + kind)

    def hold(kind):
        (api.root / ('release-' + kind)).unlink(missing_ok=True)
        signal(kind).unlink(missing_ok=True)
        (api.root / ('hold-' + kind)).touch()

    def release(kind):
        (api.root / ('hold-' + kind)).unlink(missing_ok=True)
        (api.root / ('release-' + kind)).touch()

    def private_folders():
        return list((api.root/'files').glob('.geometry-conversion-*'))

    scene = api.call('GET', imported)
    assert scene['sourceSha256'] == hashlib.sha256(original.encode('utf8')).hexdigest()
    box_max = scene['objects'][0]['bbox']['max']
    assert all(abs(actual - expected) < 1e-6 for actual, expected in zip(box_max, (.5,1,1.5), strict=True)), box_max
    guest = api.call('POST','/auth/register',{'email':'geometry-viewer@example.com','name':'Geometry viewer',
                                            'password':'isolated-geometry-viewer-123'})
    headers = {'Authorization':'Bearer '+guest['token']}
    api.call('GET', imported, extra_headers=headers, expect=404)
    org = api.call('GET','/org/mine')['org']['id']
    api.call('POST',f'/org/{org}/members',{'email':'geometry-viewer@example.com','role':'viewer'},expect=201)
    api.call('POST',f'/org/{org}/invitation:accept',{},extra_headers=headers)
    workspace = api.call('GET','/workspaces')[0]['id']
    api.call('PUT',f'/workspaces/{workspace}/sharing',{'org_id':org,'role':'viewer','enabled':True})
    assert api.call('GET', imported, extra_headers=headers)['sourceSha256'] == scene['sourceSha256']
    api.call('POST', repair, spec, extra_headers=headers, expect=404)
    assert not private_folders()

    for kind in ('repair','mesh'):
        hold(kind)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(api.call, 'POST', repair, spec, expect=409)
        b = pool.submit(api.call, 'POST', mesh, {'path':MESH})
        try:
            wait_for(lambda:signal('repair').exists() and signal('mesh').exists(), 'Real conversions did not enter pause', 22)
            assert api.call('GET',imported,expect=429)['detail']['code'] == 'geometry_conversion_busy'
            assert api.call('GET','/api/health',api_prefix=False)['ok']
            assert len(private_folders()) == 2
            put(api, 'Ref/independent.txt', 'committed while both conversion workers were occupied')
            put(api, STEP, changed)
        finally:
            release('repair');release('mesh')
        refusal = a.result(timeout=20)['detail']
        assert refusal['code'] == 'input_stale', refusal
        converted = b.result(timeout=20)
    assert raw(STEP) == changed.encode('utf8')
    raw(REPAIR,404)
    raw(VALIDATION,404)
    assert converted['points'] == 4 and converted['cells'] == 1
    assert converted['source_sha256'] == hashlib.sha256(mesh_text.encode('utf8')).hexdigest()
    preview = api.root/'verified.vtu'
    preview.write_bytes(raw(converted['path']))
    assert meshio.read(preview).cells[0].type == 'tetra'
    assert not private_folders()

    hold('repair')
    (api.root/'short-conversion').touch()
    with ThreadPoolExecutor(1) as pool:
        timed = pool.submit(api.call, 'POST', repair, spec, expect=504)
        try:
            wait_for(signal('repair').exists, 'Real repaired STEP did not reach the deadline barrier', 4)
            time.sleep(4.05)
            assert not timed.done() and len(private_folders()) == 1
        finally:
            release('repair')
            (api.root/'short-conversion').unlink()
        assert timed.result(timeout=15)['detail']['code'] == 'geometry_conversion_timeout'
    assert not private_folders()
    raw(REPAIR,404)
    raw(VALIDATION,404)

    (api.root/'limited-geometry-node').touch()
    try:
        assert api.call('POST',repair,spec,expect=409)['detail']['code'] == 'node_storage_full'
    finally:
        (api.root/'limited-geometry-node').unlink()
    result = api.call('POST',repair,spec)
    assert result['step'] == REPAIR and result['validation'] == VALIDATION
    output = api.root/'verified.step'
    output.write_bytes(raw(REPAIR))
    assert abs(cq.importers.importStep(str(output)).val().Volume() - 192) < 1e-6
    assert json.loads(raw(VALIDATION))['sourceSha256'] == hashlib.sha256(changed.encode('utf8')).hexdigest()
    api.call('POST',repair,spec,expect=409)
    assert api.call('POST',repair,{**spec,'overwrite':True})['result']['valid']
    put(api, 'A-geo/bad.msh', 'unreadable native input')
    api.call('POST',mesh,{'path':'A-geo/bad.msh'},expect=400)
    assert api.call('GET','/api/health',api_prefix=False)['ok']
    assert not private_folders()
    report = {'real_vtu_tetrahedra':1,'repaired_solid_volume':192,'scene_max':scene['objects'][0]['bbox']['max'],
              'viewer_read_only':True,'concurrency_busy_status':429,'same_case_writes_while_native_workers_paused':True,
              'replaced_source_status':409,'deadline_status':504,'no_partial_publication':True,
              'node_admission_released':True,'scratch_folders_remaining':0,'malformed_mesh_api_still_alive':True}
    (api.root/'geometry-conversion.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE, store_volume_max=5))
