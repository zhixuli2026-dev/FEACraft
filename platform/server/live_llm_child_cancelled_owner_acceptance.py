"""Actual Pandoc/MPI incomplete child registration, owner loss and cancellation."""
import asyncio,json
from app.services import process_identity_native as identity
import live_llm_remote_worker_acceptance as baseline
from live_llm_child_startup_loss_acceptance import scenario as report
from live_llm_wsl_child_startup_loss_acceptance import scenario as mpi


async def scenarios(api,worker,selected):
    await report(api,worker,selected,cancel_after_exit=True,filename='report-child-cancelled.json')
    await mpi(api,worker,selected,stage='admitted',cancel_after_exit=True,filename='mpi-child-cancelled.json')


async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
        'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'report.build':windows,'mpi.openfoam':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)


if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_child_cancelled_owner_acceptance.py',verifier=verify))
