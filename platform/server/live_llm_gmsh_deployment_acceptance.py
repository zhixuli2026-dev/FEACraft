"""Actual non-root Gmsh/OpenFOAM job, platform mesh preview and real DeepSeek."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import shlex
import sqlite3
import sys
import time

import httpx
import meshio
from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

from live_llm_acceptance import source
from live_llm_shared_index_acceptance import SharedIndexServer, configure
from live_llm_sandbox_recovery_acceptance import model_read
from testsupport import SERVER
from app.services.process_identity_native import probe

ROOT=SERVER.parents[1]
EXAMPLE=SERVER/'examples/gmsh-box'


def verify(configuration):
    api=SharedIndexServer('live-gmsh-deployment',app='live_fixture:app')
    configure(api,{},False)
    result={'passed':False,'mock_llm_calls':0,'real_llm_calls':0,'checks':[]}
    def save():
        encoded=json.dumps(result,ensure_ascii=False,indent=2)
        assert configuration['key'] not in encoded
        (api.root/'gmsh-result.json').write_text(encoded+'\n',encoding='utf-8')
    def passed(name):
        result['checks'].append(name);save();print('GMSH_PASS '+name,flush=True)
    def call(method,path,body=None,**kwargs):
        deadline=time.monotonic()+40
        while True:
            try:return api.call(method,path,body,**kwargs)
            except AssertionError as error:
                if 'storage_quota_busy' not in str(error) or time.monotonic()>=deadline:raise
                time.sleep(.2)
    print('GMSH_EVIDENCE '+str(api.root),flush=True)
    try:
        try:
            with api:
                api.create_case('Installed Gmsh deployment')
                with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},
                                  trust_env=False,timeout=120) as client:
                    response=client.get('/api/v1/runtime/capabilities');response.raise_for_status()
                    capabilities=response.json()
                # Keep this private evidence local: it contains actual paths,
                # but never copy a developer connection string to output.
                (api.root/'capabilities.json').write_text(json.dumps(capabilities,ensure_ascii=False,indent=2),encoding='utf-8')
                assert capabilities['wsl']['gmsh'] and capabilities['wsl']['openfoam']
                assert capabilities['python']['packages']['meshio']
                passed('actual_capability_probe_finds_gmsh_openfoam_and_meshio')
                for source_path,target in [('box.geo','B-mesh/gmsh/box.geo'),
                                           ('controlDict','C-run/case-gmsh/system/controlDict'),
                                           ('fvSchemes','C-run/case-gmsh/system/fvSchemes'),
                                           ('fvSolution','C-run/case-gmsh/system/fvSolution'),
                                           ('run.sh','B-mesh/gmsh/run.sh')]:
                    call('PUT',api.prefix+'/file',{'path':target,'content':(EXAMPLE/source_path).read_text(encoding='utf-8')})
                # This proof is generated only after the actual native pipeline
                # succeeds; its bytes are absent from the model prompt.
                proof_script="import pathlib,uuid\np=pathlib.Path('Ref/gmsh-proof.txt');p.parent.mkdir(exist_ok=True);p.write_text('RETIREMENT_'+uuid.uuid4().hex+'\\n')\n"
                call('PUT',api.prefix+'/file',{'path':'B-mesh/gmsh/proof.py','content':proof_script})
                settings=call('GET','/runtime/config')
                command='source '+shlex.quote(settings['openfoam_bashrc'])+' && bash B-mesh/gmsh/run.sh && python3 B-mesh/gmsh/proof.py'
                job=call('POST',api.prefix+'/jobs',{'type':'mesh','runtime':'wsl','command':command,'timeout_seconds':180})
                result['job_id']=job['id'];save()
                completed=api.wait_job(job['id'],timeout=240)
                assert completed['exit_code']==0
                output=call('GET',api.prefix+'/raw?path=C-run/case-gmsh/toolchain.json',raw=True)
                native=json.loads(output)
                assert native['uid']!=0 and native['cells']>0 and native['mesh_ok']
                assert native['gmsh_version']=='4.12.1'
                result['actual_toolchain']=native
                passed('non_root_platform_job_generates_converts_and_checks_actual_3d_mesh')
                msh=call('GET',api.prefix+'/raw?path=B-mesh/gmsh/box.msh',raw=True)
                local=api.root/'actual-box.msh';local.write_bytes(msh)
                mesh=meshio.read(local)
                tetra=sum(len(block.data) for block in mesh.cells if block.type=='tetra')
                assert tetra==native['cells'] and len(mesh.points)>0
                preview=call('POST',api.prefix+'/geometry/msh-preview',{'path':'B-mesh/gmsh/box.msh'})
                assert preview['points']==len(mesh.points) and preview['cells']==sum(len(b.data) for b in mesh.cells)
                vtu=call('GET',api.prefix+'/raw?path='+preview['path'],raw=True)
                vtu_path=api.root/'actual-preview.vtu';vtu_path.write_bytes(vtu)
                reader=vtkXMLUnstructuredGridReader();reader.SetFileName(str(vtu_path));reader.Update()
                assert reader.GetOutput().GetNumberOfPoints()==preview['points']
                assert reader.GetOutput().GetNumberOfCells()==preview['cells']
                result['mesh']={'points':preview['points'],'preview_cells':preview['cells'],'tetrahedra':tetra,
                                'msh_sha256':hashlib.sha256(msh).hexdigest(),'vtu_sha256':hashlib.sha256(vtu).hexdigest()}
                passed('real_gmsh_msh_parses_and_platform_preview_vtu_has_matching_cells')
                quality=call('POST',api.prefix+'/mesh:evaluate',{'log_path':'C-run/case-gmsh/checkMesh.log','run_dir':'C-run/case-gmsh'})
                assert quality['mesh_ok'] and quality['status']=='pass',quality
                result['quality']=quality
                boundary=call('GET',api.prefix+'/raw?path=C-run/case-gmsh/constant/polyMesh/boundary',raw=True).decode()
                assert 'walls' in boundary
                passed('actual_checkmesh_metrics_and_physical_boundary_groups_preserved')
                proof=call('GET',api.prefix+'/raw?path=Ref/gmsh-proof.txt',raw=True).decode().strip()
                result['real_llm_calls']=model_read(api,'gmsh-job',str(job['id']),proof,configuration,
                    already_restored=True,path='Ref/gmsh-proof.txt')
                passed('real_model_reads_proof_created_after_successful_native_pipeline')
        finally:
            if hasattr(api,'birth'):
                deadline=time.monotonic()+10
                while probe(api.birth)['state']=='match' and time.monotonic()<deadline:time.sleep(.05)
                result['api_exit']={'birth':api.birth,'probe':probe(api.birth)}
                assert result['api_exit']['probe']['state']=='exited'
        with closing(sqlite3.connect('file:'+str(api.root/'test.sqlite')+'?mode=ro',uri=True)) as db:
            phase,checkpoint=db.execute('SELECT phase,write_checkpoint_json FROM job_attempts WHERE job_id=?',(job['id'],)).fetchone()
            assert phase=='succeeded'
            result['job_checkpoint']=json.loads(checkpoint)
        result['passed']=True;save();print('GMSH_OK '+str(api.root/'gmsh-result.json'),flush=True)
        return 0
    except BaseException as error:
        result.update(failure_type=type(error).__name__,failure=str(error).replace(configuration['key'],'[REDACTED]')[:1200])
        save();print('GMSH_FAILED '+result['failure_type'],flush=True);return 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry']=='platform/server/live_llm_gmsh_deployment_acceptance.py' and review['classification']=='real_llm'
    for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    sys.exit(verify(asyncio.run(source(args.source_profile_id))))
