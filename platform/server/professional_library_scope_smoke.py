"""Real ParaView/CadQuery calls plus detached Windows descendants inside their Job.

The extra child is explicitly created by a fixture user script. This tests
script descendants, not a claim that stock CadQuery/VTK spawn that process.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from testsupport import IsolatedServer, SERVER
from professional_stop_smoke import CONTROLLED, members, wait_for
from professional_launch_smoke import settled
from app.services import process_identity_native as identity


def script(backend, capture):
    # Store only a fixture's own process identities; never use PID-only kills.
    setup=f'''import importlib.util,json,os,subprocess,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('scope_identity',{str(Path(identity.__file__).resolve())!r})
identity=importlib.util.module_from_spec(spec);spec.loader.exec_module(identity)
def capture_scope(value):
    child=subprocess.Popen([{getattr(sys,'_base_executable',sys.executable)!r},'-I','-S','-c','import time;time.sleep(3600)'],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP)
    records={{'parent':identity.observe(os.getpid()),'child':identity.windows_identity(int(child._handle)),
             'library':{backend!r},'value':value}}
    Path({str(capture)!r}).write_text(json.dumps(records))
    return child
'''
    if backend=='paraview':
        return setup+'''from paraview.simple import Sphere
sphere=Sphere(ThetaResolution=32,PhiResolution=32);sphere.UpdatePipeline()
child=capture_scope(sphere.GetDataInformation().GetNumberOfPoints())
while True:
    sphere.Radius=.5;sphere.UpdatePipeline();time.sleep(.01)
'''
    return setup+'''import cadquery as cq
def build_geometry(ctx):
    shape=cq.Workplane('XY').box(1,1,1)
    child=capture_scope(shape.val().Volume())
    while True:
        shape=cq.Workplane('XY').box(1,1,1);time.sleep(.01)
'''


def verify(backend):
    api=IsolatedServer('library-scope-'+backend,app='controlled_library:app')
    (api.root/'controlled_library.py').write_text(CONTROLLED,encoding='utf8')
    (api.root/'mode').write_text('cancel')
    sentinel=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-I','-S','-u','-c',
        'import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    checks=[];receipts=[]
    try:
        with api:
            for mode in ('cancel','timeout','api_death'):
                (api.root/'mode').write_text(mode)
                api.create_case('Library '+backend+' '+mode)
                capture=api.root/(mode+'-capture.json')
                source=script(backend,capture)
                path='D-post/controlled.py' if backend=='paraview' else 'A-geo/geometry.py'
                saved=api.put(path,source)
                directory=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                original=(directory/path).read_bytes()
                # Saving geometry already starts the registered auto-preview.
                # Observe that Job rather than launching a competing preview.
                if backend=='geometry':
                    job_id=saved['auto_preview_job']
                    assert type(job_id) is int
                else:
                    job_id=api.call('POST',api.prefix+'/paraview:run',{'script_path':path})['job_id']
                wait_for(lambda:capture.exists(),seconds=6)
                captured=json.loads(capture.read_text())
                assert captured['library']==backend
                assert captured['value']>100 if backend=='paraview' else abs(captured['value']-1)<1e-8
                row=api.call('GET',f'/jobs/{job_id}')
                record=row['attempt']['process_identity']
                if backend=='paraview':
                    children=row['attempt']['child_processes']['children']
                    selected=[c for c in children.values() if c['purpose']=='paraview_run' and c['phase']=='running']
                    assert len(selected)==1
                    scope_members=members(selected[0]['identity']['scope'])
                else:scope_members=members(record['scope'])
                (api.root/(mode+'-members.json')).write_text(json.dumps({'capture':captured,'members':scope_members},indent=2),encoding='utf8')
                assert all(birth in [m['identity'] for m in scope_members] for birth in (captured['parent'],captured['child']))
                assert all(identity.probe(birth)['state']=='match' for birth in (captured['parent'],captured['child']))
                if mode=='api_death':
                    owner=json.loads((api.root/'api-identity.json').read_text())
                    assert identity.probe(owner)['state']=='match'
                    (api.root/'exit-owned-api').touch()
                    wait_for(lambda:identity.probe(owner)['state']=='exited')
                    final=None
                else:
                    if mode=='cancel':assert api.call('POST',f'/jobs/{job_id}:cancel')['ok']
                    final=settled(api,job_id)
                    assert final['status']==('cancelled' if mode=='cancel' else 'failed'),final
                    stop=final['attempt']['process_stop']
                    assert stop['verified'] and stop['scope_observation']['complete'] and stop['scope_empty']
                    assert stop['reason']==('cancelled' if mode=='cancel' else 'timeout')
                    if backend=='paraview':
                        children=final['attempt']['child_processes']['children']
                        assert children and all(c['stop'] and c['stop']['verified'] for c in children.values())
                wait_for(lambda:all(identity.probe(m['identity'])['state']=='exited' for m in scope_members))
                assert sentinel.poll() is None and (directory/path).read_bytes()==original
                if backend=='geometry':
                    assert not (directory/'A-geo/scene.json').exists()
                    assert not (directory/'A-geo/exports').exists()
                receipts.append({'mode':mode,'job_id':job_id,'library_capture':captured,'members':scope_members,
                                 'final':final,'source_unchanged':True,'external_sentinel_alive':True})
                checks.append(backend+'_'+mode+'_registered_tool_and_detached_child_exited')
                (api.root/'library-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    finally:
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'actual_library':backend,'descendant_created_by_fixture_script':True,
            'cross_runtime_children_not_covered':True}
    (api.root/'library-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def main():
    root=Path(tempfile.mkdtemp(prefix='professional-library-evidence-',dir=SERVER.parents[1]/'tmp'))
    result={backend:verify(backend) for backend in ('paraview','geometry')}
    (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':sum(r['count'] for r in result.values()),'evidence':str(root)}))


if __name__=='__main__':main()
