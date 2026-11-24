"""Real native admission loss before child_prepared, plus prepared-path compatibility."""
import asyncio,json
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_wsl_child_startup_loss_acceptance import scenario


async def both(api,worker,selected):
    await scenario(api,worker,selected,stage='admitted',filename='wsl-admission-loss.json')
    await scenario(api,worker,selected)


async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
                 'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'mpi.openfoam':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=both,environment_overrides=environment)

if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_wsl_admission_loss_acceptance.py',verifier=verify))
