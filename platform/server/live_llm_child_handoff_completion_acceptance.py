"""Finish the three outstanding real scenarios after isolated store capacity failure."""
import asyncio,json,shutil
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_child_startup_loss_acceptance import scenario as windows_scenario
from live_llm_wsl_child_startup_loss_acceptance import scenario as wsl_scenario
from child_handoff_acceptance_support import CHECK,fault_for
from child_observation_acceptance_support import CHECK as observed_check

async def verify(configuration,selected,admin,observed):
    async def after_publish(api,worker,selected):
        proof=(await asyncio.to_thread(api.call,'GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)).decode().strip()
        await asyncio.to_thread(baseline.model_read,api,'','',proof,configuration,already_restored=True,path='Ref/proof.txt')
        for name in ('live-result.json','live-model-events.json'):shutil.copyfile(api.root/name,api.root/('initial-'+name))
        await wsl_scenario(api,worker,selected,stage='guardian_returned',handoff=True,fault_override=fault_for('guardian_returned'),
            cancel_after_exit=True,certificate_check=CHECK,filename='wsl-handoff-guardian_returned.json')
        await windows_scenario(api,worker,selected,stage='unobserved',certificate_check=observed_check,filename='windows-observation-compatibility.json')
        await wsl_scenario(api,worker,selected,filename='wsl-prepared-compatibility.json')
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'report.build':windows,'mpi.openfoam':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=after_publish,environment_overrides=environment,store_volume_max=16)

if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_child_handoff_completion_acceptance.py',verifier=verify))
