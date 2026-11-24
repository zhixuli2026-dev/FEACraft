"""Actual serial icoFoam rejection, input repair, and owned 2D results."""
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
import uuid

from app.services.casedict import set_block
from cae_fixtures import cavity_2d
from testsupport import IsolatedServer, SERVER
from vtkmodules.vtkIOGeometry import vtkOpenFOAMReader


RUN_DIR='C-run/case-1'
INITIAL_U=RUN_DIR+'/0/U'
TIMES=('0.1','0.2','0.3','0.4','0.5')
FAILED={'failed','interrupted','cancelled'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Cavity:
    def __init__(self,api):
        self.api=api
        self.path=api.root/'icofoam-contract.json'
        self.checks=[]
        self.jobs={}

    def save(self,**status):
        self.path.write_text(json.dumps({'checks':self.checks,'jobs':self.jobs,
            'real_model_calls':0,'mock_llm_calls':0,**status},indent=2,ensure_ascii=False),encoding='utf-8')

    def record(self,name,**data):
        self.checks.append({'name':name,**data})
        self.save(passed=False)
        print('PASS '+name,flush=True)

    def stopped(self,job_id,expected='succeeded'):
        job=self.api.wait_job(job_id,expected=expected)
        proof=job['attempt']['process_stop']
        assert proof and proof['scope_empty'] and proof['verified'],job
        self.jobs[job_id]={'type':job['type'],'status':job['status'],'stop':proof}
        return job

    def fixture(self,files,bad_u):
        api=self.api
        api.create_case('Serial icoFoam original 2D cavity')
        case=api.call('GET',api.prefix+'/caseDict')
        source=case['content']
        for block,value in {
            'caseMeta':{'discipline':'fluid','analysisType':'transient','dimensions':'2D','units':{'length':'m'}},
            'geometry':{'generator':'cadquery','parameters':{'lx':0.1,'ly':0.1,'lz':0.01}},
            'mesh':{'source':'blockMesh','qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,
                'maxAspectRatio':100,'policy':'fail'}},
            'numericalMethod':{'solver':'icoFoam'},
            'postProcessing':{'runDir':RUN_DIR},
        }.items():
            source=set_block(source,block,value)
        api.call('PUT',api.prefix+'/caseDict',{'content':source,'expected_revision':case['dict_revision']})
        for path,data in {**files,INITIAL_U:bad_u}.items():
            api.put(path,data)
        geometry="import cadquery as cq\ndef build_geometry(ctx):\n    return cq.Workplane('XY').box(0.1,0.1,0.01)\n"
        preview=api.put('A-geo/geometry.py',geometry)
        if preview.get('auto_preview_job'):
            self.stopped(preview['auto_preview_job'])
        self.case_root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))

    def solve_flow(self):
        api=self.api
        flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'solve','run_dir':RUN_DIR,
            'expected_revision':api.call('GET',api.prefix+'/caseDict')['dict_revision'],
            'budget':{'timeout_seconds':120,'max_log_bytes':2097152}},
            extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        at_b=self.wait(flow['id'],confirmation='confirm_B')
        self.confirm(at_b,'confirm_B')
        at_c=self.wait(flow['id'],confirmation='confirm_C')
        solve=self.step(at_c,'solve')
        assert solve['status']=='pending' and not solve['attempts'],solve
        return at_c

    @staticmethod
    def step(flow,key):
        return next(step for step in flow['steps'] if step['key']==key)

    def flow(self,identifier):
        return self.api.call('GET','/workflow-runs/'+identifier)

    def wait(self,identifier,*,confirmation=None,status=None):
        deadline=time.monotonic()+145
        while time.monotonic()<deadline:
            current=self.flow(identifier)
            if status and current['status']==status:
                return current
            if confirmation and self.step(current,confirmation)['status']=='awaiting_confirmation':
                for step in current['steps']:
                    if step['status']=='succeeded' and step['attempts']:
                        self.stopped(step['attempts'][-1]['job_id'])
                return self.flow(identifier)
            if current['status'] in FAILED:
                raise AssertionError(current)
            time.sleep(0.15)
        raise TimeoutError(current)

    def confirm(self,flow,key,*,expect=200,headers=None):
        step=self.step(flow,key)
        return self.api.call('POST',self.api.prefix+'/caseDict:advance',{
            'state':step['confirmation_state'],'confirmed':True,'expected_revision':step['confirmation_revision'],
            'workflow_step_id':step['id'],'expected_step_version':step['version']},
            expect=expect,extra_headers=headers)

    def output(self,path,*,expect=200,headers=None):
        return self.api.call('GET',self.api.prefix+'/raw?'+urlencode({'path':path}),
            raw=True,expect=expect,extra_headers=headers)

    def native_failure(self,at_c):
        api=self.api
        self.confirm(at_c,'confirm_C')
        failed=self.wait(at_c['id'],status='failed')
        failed_step=self.step(failed,'solve')
        assert failed_step['status']=='failed' and self.step(failed,'confirm_D')['status']=='blocked'
        job_id=failed_step['attempts'][-1]['job_id']
        job=self.stopped(job_id,'failed')
        log=api.call('GET',f'/jobs/{job_id}/log')['chunk']
        (api.root/'native-icoFoam-failure.log').write_text(log,encoding='utf-8')
        assert 'icoFoam' in log and 'FOAM FATAL IO ERROR' in log and 'movingWall' in log and 'boundaryField' in log,log
        assert job['attempt']['process_stop']['root_exit_code']!=0
        assert api.call('GET',api.prefix)['workflow_state']=='C'
        api.advance('D',expect=409)
        api.call('GET',api.prefix+'/solve/results',expect=404)
        publications=api.call('GET',api.prefix+'/solve/publications')
        assert all(not item['current'] for item in publications),publications
        self.record('actual_icofoam_bad_boundary_fails_without_publication_and_blocks_D',
            job=job_id,workflow=failed['id'],fatal_log_sha256=digest(log.encode()))
        return failed,job_id,log

    def repaired_result(self,at_d):
        api=self.api
        native=self.step(at_d,'solve')
        job=self.stopped(native['attempts'][-1]['job_id'])
        result=api.call('GET',api.prefix+'/solve/results')
        assert result['publication']['current'] and not result['publication']['stale']
        assert result['toolchain']['solver']=='icoFoam' and result['toolchain']['executable_sha256']
        assert result['toolchain']['distro']=='vibeflow-linux'
        mesh=self.step(at_d,'mesh')['publication']['id']
        assert job['attempt']['workspace']['publications'][0]['publication_id']==mesh
        evidence=result['evidence']
        assert evidence['completion_reason']=='end_time_reached'
        parsed=evidence['parsed_log']
        summary=parsed['summary']
        assert summary['finished_cleanly'] and summary['fatal_errors']==0
        assert summary['last_time']==0.5 and summary['time_points']==100
        assert set(summary['fields'])=={'Ux','Uy','p'}
        velocity=parsed['residuals']['Ux']
        assert velocity[0]>velocity[-1]>0
        files={item['path']:item for item in evidence['outputs']}
        records={}
        for field in ('U','p'):
            paths=[RUN_DIR+'/'+label+'/'+field for label in TIMES]
            assert {path for path in files if path.endswith('/'+field)}==set(paths)
            for path in paths:
                data=self.output(path)
                assert len(data)==files[path]['size'] and digest(data)==files[path]['sha256']
                records[path]=files[path]['sha256']
        marker=self.case_root/job['attempt']['workspace']['run_path']/RUN_DIR/'case-1.foam'
        assert marker.is_file()
        reader=vtkOpenFOAMReader()
        reader.SetFileName(str(marker))
        reader.CreateCellToPointOff()
        reader.UpdateInformation()
        reader.EnableAllCellArrays()
        reader.UpdateTimeStep(0.5)
        grid=reader.GetOutput().GetBlock(0)
        assert grid.GetNumberOfCells()==400
        for field,components in (('U',3),('p',1)):
            array=grid.GetCellData().GetArray(field)
            assert array.GetNumberOfComponents()==components and array.GetNumberOfTuples()==400
            assert all(math.isfinite(value) for index in range(400) for value in array.GetTuple(index))
        self.record('repaired_original_cavity_publishes_five_verified_U_and_p_outputs_and_100_residual_steps',
            solve=job['id'],mesh=mesh,publication=result['publication']['id'],cells=400,
            velocity_residual_drop_ratio=velocity[0]/velocity[-1],files=records)
        return job,result

    def run(self):
        api=self.api
        files=cavity_2d()
        valid_u=files[INITIAL_U]
        bad_u=valid_u.replace('movingWall  {','wrongMovingWall  {')
        assert bad_u!=valid_u
        self.fixture(files,bad_u)
        failed,failed_job,failed_log=self.native_failure(self.solve_flow())

        outsider=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'password':'icofoam-outsider-123','name':'Separate solver owner'})['token']
        headers={'Authorization':'Bearer '+outsider}
        api.call('PUT',api.prefix+'/file',{'path':INITIAL_U,'content':valid_u},expect=404,extra_headers=headers)
        api.call('GET',f'/jobs/{failed_job}/log',expect=404,extra_headers=headers)
        api.call('GET','/workflow-runs/'+failed['id'],expect=404,extra_headers=headers)

        api.call('PUT',api.prefix+'/file',{'path':INITIAL_U,'content':valid_u,
            'expected_sha256':digest(bad_u.encode())})
        assert self.output(INITIAL_U)==valid_u.encode()
        api.advance('A')
        at_c=self.solve_flow()
        self.confirm(at_c,'confirm_C')
        at_d=self.wait(at_c['id'],confirmation='confirm_D')
        job,result=self.repaired_result(at_d)
        published=result['publication']['id']
        for endpoint in (api.prefix+'/solve/results',api.prefix+'/solve/results?publication_id='+published,
                api.prefix+'/solve/publications',f'/jobs/{job["id"]}',f'/jobs/{job["id"]}/log/parse'):
            api.call('GET',endpoint,expect=404,extra_headers=headers)
        self.output(RUN_DIR+'/0.5/U',expect=404,headers=headers)
        self.confirm(at_d,'confirm_D',expect=404,headers=headers)
        assert api.call('GET',api.prefix)['workflow_state']=='C'
        self.record('foreign_account_cannot_repair_read_or_confirm_owned_solve')

        self.confirm(at_d,'confirm_D')
        assert self.wait(at_d['id'],status='succeeded')['status']=='succeeded'
        assert api.call('GET',api.prefix)['workflow_state']=='D'
        assert api.call('GET',f'/jobs/{failed_job}/log')['chunk']==failed_log
        assert self.flow(failed['id'])['status']=='failed'
        self.record('owner_confirms_D_after_repair_with_original_failed_attempt_preserved',
            recovery_workflow=at_d['id'],history_workflow=failed['id'])


def main():
    server=IsolatedServer('icofoam-c05')
    suite=Cavity(server)
    try:
        with server:
            suite.run()
        suite.save(passed=True,isolated_api_exited=server.proc.poll() is not None)
    except BaseException as error:
        suite.save(passed=False,error={'type':type(error).__name__,'message':str(error)})
        raise


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    main()
