"""Real report/MPI child startup loss, fresh publication and DeepSeek file reading."""
import asyncio,json
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_child_startup_loss_acceptance import scenario as windows_scenario
from live_llm_wsl_child_startup_loss_acceptance import scenario as wsl_scenario
from child_observation_acceptance_support import CHECK


async def scenarios(api,worker,selected):
    for stage in ('unobserved','created','host_ready'):
        await windows_scenario(api,worker,selected,stage=stage,certificate_check=CHECK,
            filename='windows-child-'+stage+'.json')
    for stage,cancel in [('host_ready',False),('native_ready',True)]:
        await wsl_scenario(api,worker,selected,stage=stage,cancel_after_exit=cancel,certificate_check=CHECK,
            filename='wsl-child-'+stage+'.json')
    await windows_scenario(api,worker,selected,filename='windows-child-prepared-compatibility.json')
    await wsl_scenario(api,worker,selected,filename='wsl-child-prepared-compatibility.json')


async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'report.build':windows,'mpi.openfoam':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)


if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_child_observation_loss_acceptance.py',verifier=verify))
