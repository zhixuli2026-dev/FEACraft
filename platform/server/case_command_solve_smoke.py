"""Actual legacy mesh/solve commands, downstream evidence and invalid output rejection."""
import json

from testsupport import IsolatedServer
from mpi_publication_smoke import serial_seed, RUN
from professional_launch_smoke import settled

FIXTURE='''
from fastapi import Depends
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.case_evidence import solve_gate_reason
@app.get('/api/v1/fixture/solve-gate/{case_id}/{job_id}')
async def gate(case_id:int,job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db)
 return {'reason':solve_gate_reason(case.s3_prefix,job_id,'C-run/case-1')}
'''


def main():
    api=IsolatedServer('case-command-solve',app='case_solve_fixture:app')
    (api.root/'case_solve_fixture.py').write_text(FIXTURE,encoding='utf8');checks=[]
    with api:
        initial,field=serial_seed(api)
        assert len(field)>10000
        root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        jobs=api.call('GET',api.prefix+'/jobs')
        solve=next(j for j in jobs if j['type']=='solve')
        for job in jobs:
            done=settled(api,job['id'])
            assert done['status']=='succeeded'
            assert done['attempt']['outputs']['publication']['status']=='committed'
            assert done['attempt']['workspace']['run_path']
        done=settled(api,solve['id']);publication=done['attempt']['outputs']['publication']
        evidence=json.loads((root/f'.vibeflow/jobs/job-{solve["id"]}-evidence.json').read_text())
        assert evidence['summary']['finished_cleanly']
        gate=f'/fixture/solve-gate/{api.case_id}/{solve["id"]}'
        assert api.call('GET',gate)['reason'] is None
        for path,data in initial.items():
            if '/0/' in path or '/0.orig/' in path:assert (root/path).read_bytes()==data
        reference={'command':'echo SERIAL_CURRENT','input_publications':[{'publication_id':publication['id'],'require_current':True}]}
        # Legacy proof includes .vibeflow/jobs metadata; consumers explicitly
        # select result fields instead of mounting runtime metadata as input.
        api.call('POST',api.prefix+'/jobs',reference,expect=400)
        reference['input_publications'][0]['paths']=[RUN+'/0.5/U']
        assert settled(api,api.call('POST',api.prefix+'/jobs',reference)['id'])['status']=='succeeded'
        checks.append('real_blockMesh_icoFoam_fields_legacy_gate_and_current_publication')
        (root/RUN/'0.5/U').write_bytes(field+b'\n// later user edit\n')
        assert api.call('GET',gate)['reason'] is not None
        api.call('POST',api.prefix+'/jobs',reference,expect=409)
        assert (root/'.vibeflow/publications'/publication['id']/'files'/RUN/'0.5/U').read_bytes()==field
        checks.append('edited_field_invalidates_legacy_gate_and_keeps_immutable_history')
        for mode in ('not_solver','malformed_field'):
            api.create_case('typed solve '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            for path,data in initial.items():
                target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            command='echo NOT_A_SOLVER' if mode=='not_solver' else (
                'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && icoFoam && printf broken > 0.5/U')
            job=api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl','run_dir':RUN,'command':command})
            done=settled(api,job['id'])
            assert done['status']=='failed',(mode,done['status'])
            assert done['attempt']['outputs']['publication']['status']!='committed'
            assert not (root/RUN/'0.5/U').exists()
            assert not (root/f'.vibeflow/jobs/job-{job["id"]}-evidence.json').exists()
            assert all((root/path).read_bytes()==data for path,data in initial.items())
            checks.append(mode+'_cannot_publish_solve_evidence_or_fields')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (api.root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print('CASE_COMMAND_SOLVE_OK '+json.dumps(result))


if __name__=='__main__':main()
