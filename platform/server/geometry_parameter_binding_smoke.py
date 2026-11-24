"""Native parameter/volume/topology binding and failed-current-source gates."""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from app.services.casedict import set_block
from testsupport import IsolatedServer
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME

SCRIPT='''import cadquery as cq
def build_geometry(ctx):
    width=float(ctx.params['width'])
    if width<=0:raise ValueError('width must be positive')
    return {'parameter_box':cq.Workplane('XY').box(width,2,3)}
'''


def api_for(settings,peer=None):
    api=IsolatedServer('geometry-parameters',app='parameter_fixture:app')
    overrides=dict(settings)
    if peer:
        overrides.update({key:peer.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')})
        api.token=peer.token
    (api.root/'parameter_fixture.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf8')
    return api


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    checks=[]
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        settings=store.settings() if store else {}
        with api_for(settings) as api:
            api.create_case('N3 parameter binding');prefix=api.prefix
            def parameters(width):
                saved=api.call('GET',prefix+'/caseDict')
                content=set_block(saved['content'],'geometry',{'parameters':{'width':width}})
                api.call('PUT',prefix+'/caseDict',{'content':content,'expected_revision':saved['dict_revision']})
            def document(name,identifier=None,reader=api):
                return reader.call('GET',prefix+'/geometry/'+name+('?publication_id='+identifier if identifier else ''))
            def measure(width,identifier=None,reader=api):
                scene,validation=(document(name,identifier,reader) for name in ('scene','validation'))
                assert scene['publication']['id']==validation['publication']['id']
                box=scene['objects'][0]['bbox'];assert abs(box['max'][0]-box['min'][0]-width)<1e-6
                solid=validation['objects'][0]
                assert abs(solid['volume']-width*6)<1e-8
                assert solid['counts']=={'solids':1,'shells':1,'faces':6,'edges':12,'vertices':8}
                assert solid['valid'] and solid['watertight'] and solid['manifold']
                assert validation['source_sha256']==hashlib.sha256(SCRIPT.encode()).hexdigest()
                return validation
            parameters(2)
            created=api.put('A-geo/geometry.py',SCRIPT);api.wait_job(created['auto_preview_job'])
            first=measure(2);first_id=first['publication']['id']
            assert first['publication']['current'] and not first['publication']['stale']
            checks.append('current_parameters_generate_native_12_volume_and_closed_box_topology')
            parameters(4)
            assert document('validation')['publication']['stale']
            api.advance('B',expect=409)
            old=measure(2,first_id);assert old['publication']['stale']
            checks.append('parameter_edit_invalidates_B_gate_but_retains_original_12_volume_snapshot')
            build=api.call('POST',prefix+'/geometry:export',{});api.wait_job(build['job_id'])
            second=measure(4);second_id=second['publication']['id']
            assert second_id!=first_id and second['case_input_sha256']!=first['case_input_sha256']
            assert not second['publication']['stale']
            step=api.call('GET',prefix+'/raw?path='+quote('A-geo/exports/parameter_box.step'),raw=True)
            path=api.root/'native-export.step';path.write_bytes(step)
            import cadquery as cq
            shape=cq.importers.importStep(str(path)).val()
            assert abs(shape.Volume()-24)<1e-8 and len(shape.Faces())==6
            api.advance('B');api.advance('A')
            checks.append('regenerated_export_matches_24_volume_native_STEP_and_allows_explicit_B_confirmation')
            parameters(-1)
            failed=api.call('POST',prefix+'/geometry:preview',{})
            api.wait_job(failed['job_id'],expected='failed')
            current=measure(4)
            assert current['publication']['id']==second_id and current['publication']['stale']
            api.advance('B',expect=409)
            history=api.call('GET',prefix+'/geometry/publications')
            assert any(row['id']==failed['publication_id'] and row['status']=='failed' for row in history)
            checks.append('failed_current_geometry_preserves_old_scene_but_cannot_advance_with_old_proof')
            if store:
                with api_for(settings,api) as reader:
                    assert reader.root!=api.root
                    for width,identifier in ((2,first_id),(4,second_id)):
                        assert measure(width,identifier,reader)['publication']['stale']
                    saved=reader.call('GET',prefix+'/caseDict')
                    reader.call('POST',prefix+'/caseDict:advance',{'state':'B','confirmed':True,'expected_revision':saved['dict_revision']},expect=409)
                    checks.append('fresh_S3_API_preserves_both_measured_versions_and_rejects_failed_current_gate')
        result={'passed':True,'count':len(checks),'checks':checks,'backend':'s3' if store else 'fs','database':'sqlite'}
        (api.root/'parameter-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print('GEOMETRY_PARAMETERS_OK '+json.dumps({'evidence':str(api.root),**result}),flush=True)


if __name__=='__main__':main()
