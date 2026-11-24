"""Real isolated OpenFOAM and Gmsh quality gates through the production API."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
import uuid

from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from cae_fixtures import cavity_2d
from testsupport import IsolatedServer, SERVER
from vtkmodules.vtkFiltersVerdict import vtkMeshQuality
from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader


DEFAULT_TARGETS={'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,
                 'minJacobian':0.6,'minVolume':1e-9,'policy':'fail'}
VIOLATING_TARGETS={'maxNonOrtho':1,'maxSkewness':0.00001,'maxAspectRatio':0.5,
                   'minJacobian':0.99,'minVolume':1e-6}
METRICS={'non_orthogonality','skewness','aspect_ratio','jacobian','minimum_volume'}
STAGES={'failed','interrupted','cancelled'}
STANDARD_RUN='C-run/case-1'
THERMAL_RUN='C-run/conduction'


class Acceptance:
    def __init__(self,api):
        self.api=api
        self.checks=[]
        self.jobs={}
        self.evidence=api.root/'mesh-quality-results.json'

    def save(self,**status):
        self.evidence.write_text(json.dumps({'checks':self.checks,'jobs':self.jobs,
            'real_model_calls':0,'mock_llm_calls':0,**status},ensure_ascii=False,indent=2),encoding='utf-8')

    def record(self,name,**evidence):
        self.checks.append({'name':name,**evidence})
        self.save(passed=False)
        print('PASS '+name,flush=True)

    def update_targets(self,targets,source='blockMesh',solver='icoFoam',run_dir=STANDARD_RUN):
        api=self.api
        previous=api.call('GET',api.prefix+'/caseDict')
        updated=set_block(previous['content'],'mesh',{'source':source,'qualityTargets':targets})
        updated=set_block(updated,'numericalMethod',{'solver':solver})
        updated=set_block(updated,'postProcessing',{'runDir':run_dir})
        api.call('PUT',api.prefix+'/caseDict',{'content':updated,'expected_revision':previous['dict_revision']})

    def fixture(self,name,targets,*,deformed=False,thermal=False,source='blockMesh'):
        api=self.api
        api.create_case(name)
        run_dir=THERMAL_RUN if thermal else STANDARD_RUN
        self.update_targets(targets,source,'laplacianFoam' if thermal else 'icoFoam',run_dir)
        inputs=thermal_files() if thermal else cavity_2d()
        if source=='gmsh':
            del inputs[run_dir+'/system/blockMeshDict']
            inputs[run_dir+'/system/mesh.geo']=(SERVER/'examples/gmsh-conduction/mesh.geo').read_text(encoding='utf-8')
        if deformed:
            path=run_dir+'/system/blockMeshDict'
            inputs[path]=inputs[path].replace('(1 1 0) (0 1 0)','(1.52 1 0) (0.20 1 0)').replace(
                         '(1 1 0.1) (0 1 0.1)','(1.52 1 0.1) (0.20 1 0.1)')
        for relative,contents in inputs.items():
            api.put(relative,contents)
        geometry="import cadquery as cq\ndef build_geometry(ctx):\n    return cq.Workplane('XY').box(1,1,1)\n"
        written=api.put('A-geo/geometry.py',geometry)
        if written.get('auto_preview_job'):
            api.wait_job(written['auto_preview_job'])
        self.root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        return run_dir

    def raw(self,path,expect=200,headers=None):
        return self.api.call('GET',self.api.prefix+'/raw?'+urlencode({'path':path}),
            expect=expect,raw=True,extra_headers=headers)

    def submit(self,target,run_dir):
        api=self.api
        return api.call('POST',api.prefix+'/workflow-runs',{'target':target,'run_dir':run_dir,
            'expected_revision':api.call('GET',api.prefix+'/caseDict')['dict_revision'],
            'budget':{'timeout_seconds':120,'max_log_bytes':2097152}})

    @staticmethod
    def step(run,key):
        return next(item for item in run['steps'] if item['key']==key)

    def workflow(self,run):
        return self.api.call('GET',f'/workflow-runs/{run["id"]}')

    def wait(self,run,*,confirmation=None,status=None):
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            current=self.workflow(run)
            if current['status']==status:
                return current
            if confirmation and self.step(current,confirmation)['status']=='awaiting_confirmation':
                for item in current['steps']:
                    if item['status']=='succeeded' and item['attempts']:
                        self.job(item)
                return self.workflow(run)
            if current['status'] in STAGES:
                raise AssertionError(current)
            time.sleep(0.15)
        raise TimeoutError(current)

    def job(self,step,status='succeeded'):
        number=step['attempts'][-1]['job_id']
        job=self.api.wait_job(number,expected=status)
        self.save_job(job,step['key'])
        return job

    def save_job(self,job,stage):
        stop=job['attempt']['process_stop']
        assert stop and stop['verified'],job
        self.jobs[job['id']]={'step':stage,'status':job['status'],'stop':stop}

    def confirm(self,run,key,expect=200,headers=None):
        item=self.step(run,key)
        return self.api.call('POST',self.api.prefix+'/caseDict:advance',{
            'state':item['confirmation_state'],'confirmed':True,'expected_revision':item['confirmation_revision'],
            'workflow_step_id':item['id'],'expected_step_version':item['version']},
            expect=expect,extra_headers=headers)

    def wait_stage(self,expected):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            case=self.api.call('GET',self.api.prefix)
            if case['workflow_state']==expected:
                return case
            time.sleep(0.15)
        raise AssertionError(case)

    def through_mesh(self,run):
        waiting=self.wait(run,confirmation='confirm_B')
        self.confirm(waiting,'confirm_B')
        return waiting

    def mesh(self,expect=200,headers=None,query=''):
        return self.api.call('GET',self.api.prefix+'/mesh/results'+query,expect=expect,extra_headers=headers)

    def verify_measurement(self,result,run_dir,expected_cells,expected_type):
        quality=result['quality']
        log=self.raw(run_dir+'/checkMesh.log')
        assert hashlib.sha256(log).hexdigest()==quality['log_sha256'] and b'Mesh OK.' in log
        assert set(quality['metrics'])==METRICS and quality['case_dict_mesh_hash']
        source=quality['jacobian_evidence']['source']
        assert quality['jacobian_evidence']['method']=='verdict-scaled-jacobian'
        assert quality['jacobian_evidence']['volume_cells']==expected_cells
        content=self.raw(source['path'])
        assert len(content)==source['size'] and hashlib.sha256(content).hexdigest()==source['sha256']
        actual=self.api.root/f'case-{self.api.case_id}-native-quality.vtu'
        actual.write_bytes(content)
        reader=vtkXMLUnstructuredGridReader()
        reader.SetFileName(str(actual))
        reader.Update()
        mesh=reader.GetOutput()
        assert Counter(mesh.GetCellType(i) for i in range(mesh.GetNumberOfCells()))=={expected_type:expected_cells}
        verdict=vtkMeshQuality()
        verdict.SetInputData(mesh)
        verdict.SetHexQualityMeasureToScaledJacobian()
        verdict.SetTetQualityMeasureToScaledJacobian()
        verdict.Update()
        values=verdict.GetOutput().GetCellData().GetArray('Quality')
        independently_measured=min(values.GetValue(i) for i in range(values.GetNumberOfValues()))
        assert math.isclose(quality['metrics']['jacobian'],independently_measured,abs_tol=2e-6),quality
        assert result['toolchain']['executable_sha256']['foamToVTK'] and result['toolchain']['executable_sha256']['checkMesh']
        (self.api.root/f'case-{self.api.case_id}-checkMesh.log').write_bytes(log)
        return quality['metrics']

    def rejected_job(self,run,run_dir):
        failed=self.wait(run,status='failed')
        assert self.step(failed,'solve')['status']=='blocked' and not self.step(failed,'solve')['attempts']
        step=self.step(failed,'mesh')
        assert step['status']=='failed'
        job=self.job(step,status='failed')
        attempt=job['attempt']['workspace']
        private=self.root/attempt['run_path']
        quality=json.loads((private/'B-mesh/quality.json').read_text(encoding='utf-8'))
        log=(private/run_dir/'checkMesh.log').read_text(encoding='utf-8')
        self.mesh(expect=404)
        assert self.api.call('GET',self.api.prefix)['workflow_state']=='B'
        return quality,log

    def main(self):
        api=self.api
        run_dir=self.fixture('Native quality admits actual solve',DEFAULT_TARGETS)
        api.advance('C',expect=409)
        run=self.through_mesh(self.submit('solve',run_dir))
        ready=self.wait(run,confirmation='confirm_C')
        assert self.step(ready,'solve')['status']=='pending' and not self.step(ready,'solve')['attempts']
        report=self.mesh()
        identifier=report['publication']['id']
        measured=self.verify_measurement(report,run_dir,400,12)
        assert abs(measured['jacobian']-1)<1e-6 and report['quality']['gate']=={'status':'pass'}
        assert api.call('POST',api.prefix+'/mesh:evaluate',{})==report['quality']
        self.record('native_blockmesh_five_metrics_and_current_evaluate',metrics=measured,publication=identifier)

        outsider=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'mesh-owner-fixture-123','name':'Separate owner'})['token']
        headers={'Authorization':'Bearer '+outsider}
        self.mesh(expect=404,headers=headers,query='?publication_id='+identifier)
        api.call('POST',api.prefix+'/mesh:evaluate',{},expect=404,extra_headers=headers)
        self.confirm(ready,'confirm_C',expect=404,headers=headers)
        self.raw(report['quality']['jacobian_evidence']['source']['path'],expect=404,headers=headers)
        self.record('outsider_cannot_read_or_evaluate_mesh_or_confirm_solver')

        proof=self.root/report['quality']['jacobian_evidence']['source']['path']
        original=proof.read_bytes()
        proof.write_bytes(original+b'\n')
        try:
            self.confirm(ready,'confirm_C',expect=409)
            api.call('POST',api.prefix+'/mesh:evaluate',{},expect=409)
            assert self.step(self.workflow(run),'solve')['status']=='pending'
            assert self.mesh(query='?publication_id='+identifier)['quality']==report['quality']
        finally:
            proof.write_bytes(original)
        self.confirm(ready,'confirm_C')
        solved=self.wait(run,confirmation='confirm_D')
        solve=self.job(self.step(solved,'solve'))
        assert solve['attempt']['workspace']['publications'][0]['publication_id']==identifier
        assert api.call('GET',api.prefix+'/solve/results')['toolchain']['solver']=='icoFoam'
        self.record('damaged_evidence_blocks_confirmed_solve_then_restored_input_runs_icoFoam')

        api.call('POST',f'/workflow-runs/{run["id"]}:cancel',{'expected_version':solved['version']})
        self.update_targets({**DEFAULT_TARGETS,'minJacobian':0.8,'minVolume':1e-6})
        assert self.mesh(query='?publication_id='+identifier)['publication']['stale']
        geometry=api.call('POST',api.prefix+'/geometry:preview')
        self.save_job(api.wait_job(geometry['job_id']),'geometry')
        mesh_job=self.step(ready,'mesh')['attempts'][-1]['job_id']
        operations=api.call('GET',f'/jobs/{mesh_job}/operations')
        assert operations['rerun']['available'],operations
        repeated=api.call('POST',f'/jobs/{mesh_job}:repeat',{'action':'rerun',
            'parameters':{'run_dir':run_dir},**{key:operations[key] for key in ('source_attempt_id',
                'source_version','expected_revision','expected_event_sequence')}},
            expect=202,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        self.save_job(api.wait_job(repeated['id'],expected='failed'),'mesh')
        self.wait_stage('B')
        self.record('real_failed_mesh_rerun_returns_from_C_to_B')
        api.advance('C',expect=409)
        self.update_targets({**DEFAULT_TARGETS,'minJacobian':0.8})
        geometry=api.call('POST',api.prefix+'/geometry:preview')
        self.save_job(api.wait_job(geometry['job_id']),'geometry')
        updated=self.wait(self.submit('mesh',run_dir),confirmation='confirm_C')
        assert self.mesh()['publication']['id']!=identifier
        assert self.mesh(query='?publication_id='+identifier)['quality']==report['quality']
        self.confirm(updated,'confirm_C')
        self.record('changed_targets_block_stale_quality_until_regenerated_history_preserved')

        cases=[('fail',None,False),('warn',None,True),
            ('allowWithReason',None,False),('allowWithReason','Measured waiver for coarse comparison',True)]
        for policy,reason,accepted in cases:
            targets={**VIOLATING_TARGETS,'policy':policy}
            if reason:
                targets['reason']=reason
            run_dir=self.fixture(f'Deformed mesh {policy} {bool(reason)}',targets,deformed=True)
            run=self.through_mesh(self.submit('mesh' if accepted else 'solve',run_dir))
            if accepted:
                ready=self.wait(run,confirmation='confirm_C')
                published=self.mesh()
                quality=published['quality']
                self.verify_measurement(published,run_dir,400,12)
                self.confirm(ready,'confirm_C')
                assert api.call('GET',api.prefix)['workflow_state']=='C'
            else:
                quality,log=self.rejected_job(run,run_dir)
                assert 'Mesh OK.' in log and quality['gate']['status']=='blocked'
            assert quality['status']=='fail' and quality['mesh_ok'] and 0<quality['metrics']['jacobian']<0.99
            assert {item['metric'] for item in quality['violations']}==METRICS,quality
            if policy=='warn':
                assert quality['gate']=={'status':'warn'}
            if reason:
                assert quality['gate']=={'status':'allowed_with_reason','reason':reason}
            self.record('measured_gate_'+policy+('_with_reason' if reason else ''),quality=quality)

        run_dir=self.fixture('Native checkMesh failure cannot be waived',{'minJacobian':0.1,'policy':'warn'},thermal=True)
        run=self.through_mesh(self.submit('solve',run_dir))
        quality,log=self.rejected_job(run,run_dir)
        assert 'Failed 1 mesh checks.' in log and 'Mesh OK.' not in log
        assert quality['mesh_ok'] is False and quality['gate']['status']=='blocked'
        assert quality['violations']==[{'metric':'jacobian','reason':'missing_metric','target':0.1}]
        self.record('actual_checkmesh_failure_and_missing_measurement_block_warn',quality=quality)

        run_dir=self.fixture('Gmsh final OpenFOAM mesh',DEFAULT_TARGETS,thermal=True,source='gmsh')
        run=self.through_mesh(self.submit('mesh',run_dir))
        ready=self.wait(run,confirmation='confirm_C')
        published=self.mesh()
        metrics=self.verify_measurement(published,run_dir,64,12)
        assert abs(metrics['jacobian']-1)<1e-6
        assert published['toolchain']['mesh_source']=='gmsh' and published['toolchain']['executable_sha256']['gmshToFoam']
        self.confirm(ready,'confirm_C')
        self.record('actual_gmsh_converted_final_polymesh_matches_independent_verdict',metrics=metrics)


def main():
    server=IsolatedServer('mesh-quality-b16')
    suite=Acceptance(server)
    try:
        with server:
            suite.main()
            suite.save(passed=True)
    except BaseException as error:
        suite.save(passed=False,error={'type':type(error).__name__,'message':str(error)})
        raise


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    main()
