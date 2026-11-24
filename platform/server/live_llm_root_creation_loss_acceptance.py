"""Actual creation/host-only startup loss with revoked late native admission."""
import asyncio,json
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_root_startup_loss_acceptance import scenario

async def scenarios(api,worker,selected):
    for runtime,stage,cancel in [('windows','created',False),('wsl','created',False),
            ('wsl','host_ready',False),('wsl','host_ready',True),('wsl','native_ready',False)]:
        await scenario(api,worker,selected,runtime=runtime,stage=stage,cancel=cancel)

async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({
        'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'command.geometry':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)

if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_root_creation_loss_acceptance.py',verifier=verify))
