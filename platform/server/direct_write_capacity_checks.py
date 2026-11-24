"""Actual FS/S3 file writers, native CAD/VTK/OpenFOAM and WebSocket; no LLM."""
import argparse
import asyncio
import hashlib
import json
import shlex
import subprocess
import sys
from urllib.parse import quote

import httpx
from websockets.sync.client import connect

from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres

OBSERVE = '''
import asyncio
import threading
from app import storage
from app.services import file_transactions,owner_storage_budget
from app.routers import cadscene
write=file_transactions.write
finish=file_transactions.finish_committed
entered=threading.Event()
release=threading.Event()
def record(stage,case):
 pool=owner_storage_budget.current()
 assert not storage._use_s3() or (pool is not None and not pool.closed)
 with (root/'boundaries.jsonl').open('a') as stream:
  stream.write(json.dumps({'stage':stage,'case_id':case.id,'owner':pool.ticket['user_id'] if pool else None})+'\\n')
async def writing(db,case,files,**kwargs):
 record('write',case)
 result=await write(db,case,files,**kwargs)
 if (root/'write-failure').exists():raise OSError('intentional failure after real file transaction')
 return result
async def finishing(db,case):
 record('finish_enter',case)
 if (root/'hold-finish').exists():
  entered.set()
  assert await asyncio.to_thread(release.wait,15),'test finish barrier expired'
 result=await finish(db,case)
 record('finish_exit',case)
 return result
file_transactions.write=writing
file_transactions.finish_committed=finishing
@app.post('/api/v1/fixture/cancel-scene/{case_id}')
async def cancel_scene(case_id:int,user=Depends(get_current_user)):
 entered.clear();release.clear();(root/'hold-finish').touch()
 task=asyncio.create_task(cadscene._write_scene(case_id,user.id,{'objects':[],'proof':'cancel-drained'},{}))
 try:
  async with asyncio.timeout(15):
   while not entered.is_set():
    if task.done():await task
    await asyncio.sleep(.01)
  task.cancel();await asyncio.sleep(.05)
  assert not task.done(),'writer exited before real finish drained'
  release.set()
  try:await task
  except asyncio.CancelledError:pass
  else:raise AssertionError('cancellation was swallowed')
  return {'cancelled':True,'drained':True}
 finally:
  release.set();(root/'hold-finish').unlink(missing_ok=True)
  if not task.done():
   task.cancel()
   try:await task
   except asyncio.CancelledError:pass
'''


def native_inputs(root):
    import cadquery as cq
    import meshio
    from cae_benchmarks import thermal_files
    cq.exporters.export(cq.Workplane('XY').box(1, 2, 3), str(root / 'box.step'))
    meshio.write_points_cells(root / 'tetra.msh', [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
                              [('tetra', [[0, 1, 2, 3]])], file_format='gmsh22', binary=False)
    inputs = thermal_files()
    for path, content in inputs.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf8')
    run = root / 'C-run/conduction'
    linux = '/mnt/' + run.drive[0].lower() + run.as_posix()[2:]
    command = 'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd ' + shlex.quote(linux) + ' && blockMesh && checkMesh'
    result = subprocess.run(['wsl.exe', '--', 'bash', '-lc', command], capture_output=True, timeout=90)
    (root / 'actual-checkMesh.log').write_bytes(result.stdout)
    (root / 'actual-checkMesh.stderr').write_bytes(result.stderr)
    assert result.returncode == 0 and b'Mesh OK' in result.stdout, 'Actual OpenFOAM failed; inspect log'
    inputs['C-run/conduction/checkMesh.log'] = result.stdout.decode('utf8')
    inputs['A-geo/box.step'] = (root / 'box.step').read_text(encoding='utf8')
    inputs['A-geo/tetra.msh'] = (root / 'tetra.msh').read_text(encoding='utf8')
    return inputs


def verify(store=None, database_url=None, group='writers'):
    api = IsolatedServer('direct-write-capacity', app='direct_fixture:app', database_url=database_url)
    if store:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    settings = store.settings() if store else {}
    (api.root / 'direct_fixture.py').write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE + OBSERVE, encoding='utf8')
    result = {'passed': False, 'database': 'postgresql' if database_url else 'sqlite',
              'storage': 's3' if store else 'fs', 'llm_calls': 0, 'checks': []}
    def save():
        (api.root / 'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    def check(name, **fields):
        result['checks'].append({'name': name, **fields})
        save()
        print('DIRECT_WRITE_PASS ' + name, flush=True)
    def credit():
        state = api.call('GET', '/fixture/credit')
        if store:
            assert state['rows'] and all(r['state'] == 'settled' and r['receipt']['closed'] for r in state['rows']), state
    def raw(path):
        return api.call('GET', api.prefix + '/raw?path=' + quote(path, safe=''), raw=True)
    def socket():
        return connect(api.base.replace('http:', 'ws:') + '/ws/cadscene?case_id=' + str(api.case_id) + '&token=' + api.token,
                       proxy=None, open_timeout=15)
    def receive(ws):
        return json.loads(ws.recv(timeout=30))
    print('DIRECT_WRITE_EVIDENCE ' + str(api.root), flush=True)
    try:
        inputs = native_inputs(api.root)
        with api:
            api.create_case('Direct file capacity')
            owner = api.call('GET', '/auth/me')['id']
            for path, content in inputs.items():
                api.put(path, content)
            routes = [
                ('/udf/scaffold', {'name': 'capacityUdf'}),
                ('/mesh:evaluate', {'run_dir': 'C-run/conduction', 'log_path': 'C-run/conduction/checkMesh.log'}),
                ('/geometry/repair', {'path': 'A-geo/box.step', 'operation': 'scale', 'factor': 2}),
                ('/geometry/msh-preview', {'path': 'A-geo/tetra.msh'}),
            ]
            with socket() as ws:
                assert receive(ws)['type'] == 'ready'
                if store:
                    before = api.call('GET', '/fixture/credit?measure=true')['bytes']
                    (api.root / 'quota-zero').touch()
                    try:
                        for path, body in routes:
                            api.call('POST', api.prefix + path, body, expect=429)
                            credit()
                            check('zero_quota' + path)
                        ws.send(json.dumps({'type': 'scene.clear'}))
                        refused = receive(ws)
                        assert refused['status'] == 429 and refused['code'] == 'storage_capacity', refused
                        credit()
                        assert api.call('GET', '/fixture/credit?measure=true')['bytes'] == before
                        check('zero_quota_websocket_and_all_routes_no_physical_growth')
                    finally:
                        (api.root / 'quota-zero').unlink()
                obj = {'name': 'real-scene', 'vertices': [[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'faces': [[0, 1, 2]]}
                ws.send(json.dumps({'type': 'scene.replace', 'objects': [obj]}))
                saved = receive(ws)
                assert saved['type'] == 'scene.replace', saved
                data = raw('A-geo/scene.json')
                assert hashlib.sha256(data).hexdigest() == saved['content_sha256']
                assert json.loads(data)['objects'][0]['name'] == 'real-scene'
                credit()
                check('same_websocket_retry_saves_actual_scene')
                ws.send(json.dumps({'type': 'scene.clear', 'expected_sha256': '0' * 64}))
                conflict = receive(ws)
                assert conflict['type'] == 'error' and raw('A-geo/scene.json') == data, conflict
                credit()
                ws.send(json.dumps({'type': 'scene.clear', 'expected_sha256': saved['content_sha256']}))
                assert receive(ws)['type'] == 'scene.clear'
                assert not json.loads(raw('A-geo/scene.json'))['objects']
                credit()
                check('scene_conflict_preserves_bytes_then_clear_succeeds')
            udf = api.call('POST', api.prefix + routes[0][0], routes[0][1])
            assert len(udf['files']) == 9
            for path in udf['files']:
                assert raw('UDF/capacityUdf/' + path)
            credit()
            api.call('POST', api.prefix + routes[0][0], routes[0][1], expect=409)
            credit()
            check('udf_nine_actual_files_and_collision')
            quality = api.call('POST', api.prefix + routes[1][0], routes[1][1])
            assert quality['mesh_ok'] and quality['metrics']['aspect_ratio'] >= 1, quality
            assert quality['log_sha256'] == hashlib.sha256(inputs['C-run/conduction/checkMesh.log'].encode()).hexdigest()
            assert json.loads(raw('B-mesh/quality.json')) == quality
            credit()
            check('actual_blockMesh_checkMesh_quality', metrics=quality['metrics'])
            repaired = api.call('POST', api.prefix + routes[2][0], routes[2][1])
            target = api.root / 'downloaded.step'
            target.write_bytes(raw(repaired['step']))
            import cadquery as cq
            volume = cq.importers.importStep(str(target)).val().Volume()
            assert abs(volume - 48) < 1e-6, volume
            validation = json.loads(raw(repaired['validation']))
            assert validation['sourceSha256'] == hashlib.sha256(inputs['A-geo/box.step'].encode()).hexdigest()
            credit()
            api.call('POST', api.prefix + routes[2][0], routes[2][1], expect=409)
            credit()
            check('actual_cadquery_scaled_step_and_collision', volume=volume)
            preview = api.call('POST', api.prefix + routes[3][0], routes[3][1])
            assert preview['points'] == 4 and preview['cells'] == 1, preview
            data = raw(preview['path'])
            target = api.root / 'downloaded.vtu'
            target.write_bytes(data)
            import meshio
            parsed = meshio.read(target)
            assert len(parsed.points) == 4 and len(parsed.cells_dict['tetra']) == 1
            credit()
            check('actual_meshio_vtk_tetra_roundtrip')
            for route, body, status in [
                ('/geometry/repair', {'path': 'A-geo/missing.step'}, 404),
                ('/geometry/msh-preview', {'path': 'A-geo/box.step'}, 400),
                ('/mesh:evaluate', {'log_path': 'missing.log'}, 404),
            ]:
                api.call('POST', api.prefix + route, body, expect=status)
                credit()
                check('error_settles' + route)
            (api.root / 'write-failure').touch()
            try:
                api.call('POST', api.prefix + '/udf/scaffold', {'name': 'recoveryUdf'}, expect=500, raw=True)
            finally:
                (api.root / 'write-failure').unlink()
            credit()
            api.call('POST', api.prefix + '/udf/scaffold', {'name': 'recoveryUdf'})
            assert b'functionObject' in raw('UDF/recoveryUdf/myFunctionObject.H')
            credit()
            check('actual_uncommitted_write_recovered_then_retry')
            cancelled = api.call('POST', '/fixture/cancel-scene/' + str(api.case_id), {})
            assert cancelled == {'cancelled': True, 'drained': True}
            assert json.loads(raw('A-geo/scene.json'))['proof'] == 'cancel-drained'
            credit()
            check('cancel_during_finish_drains_before_credit_release')
            events = [json.loads(line) for line in (api.root / 'boundaries.jsonl').read_text().splitlines()]
            assert {'write', 'finish_enter', 'finish_exit'} <= {e['stage'] for e in events}
            assert all(e['owner'] == (owner if store else None) for e in events)
            check('observed_actual_write_and_finish_scopes', count=len(events))
            result['passed'] = True
            save()
    except BaseException as error:
        result['failure'] = {'type': type(error).__name__, 'detail': str(error)[:2500]}
        save()
        print('DIRECT_WRITE_FAILED ' + json.dumps(result['failure']), flush=True)
        return 1
    print('DIRECT_WRITE_OK ' + str(api.root / 'checks.json'), flush=True)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', choices=('sqlite', 'postgresql'), default='sqlite')
    parser.add_argument('--storage', choices=('fs', 's3'), default='s3')
    args = parser.parse_args()
    if args.storage == 's3':
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed') as store:
            code = asyncio.run(postgres(store, 'writers', verify)) if args.database == 'postgresql' else verify(store)
    else:
        code = asyncio.run(postgres(None, 'writers', verify)) if args.database == 'postgresql' else verify()
    sys.exit(code)
