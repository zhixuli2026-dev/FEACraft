"""Actual namespace-before-registration loss, pending cancellation and original window."""
import asyncio
import live_llm_remote_worker_acceptance as baseline
from live_llm_remote_startup_loss_acceptance import scenario

FAULT='''
from app.services import job_launch_registration,job_linux_namespaces
async def actual_crash(owner,stage):
 root=Path(FAULT_ROOT);armed=root/'armed'
 if armed.exists() and armed.read_text()==stage:
  armed.unlink()
  path=root/'prepared.json';staged=path.with_suffix('.staged')
  staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'stage':stage}))
  staged.replace(path)
  while not (root/'crash-now').exists():await asyncio.sleep(.02)
  os._exit(17)
original_namespace=job_linux_namespaces.prepared
async def namespace_prepared(owner,process,init,**kwargs):
 await original_namespace(owner,process,init,**kwargs)
 if kwargs.get('child_id') is None:await actual_crash(owner,'namespace')
job_linux_namespaces.prepared=namespace_prepared
original_launch=job_launch_registration.prepared
async def launch_prepared(owner,process):
 await original_launch(owner,process)
 await actual_crash(owner,'launch')
job_launch_registration.prepared=launch_prepared
'''

class BoundaryWorker(baseline.LinuxWorker):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.api.env['VIBEFLOW_JOB_HEARTBEAT_SECONDS']='1'
    def fixture_code(self,root):return 'FAULT_ROOT='+repr(baseline.wsl_path(root))+'\n'+FAULT

async def boundaries(api,worker,selected):
    for stage,cancel,name in [('namespace',False,'namespace-startup-loss.json'),
                              ('namespace',True,'cancelled-startup-loss.json'),
                              ('launch',False,'startup-loss.json')]:
        # The previous scenario's finally releases only its already stopped worker.
        # A fresh scenario requires fresh controls on the current live worker.
        for filename in ['crash-now','prepared.json']:
            (worker.root/filename).unlink(missing_ok=True)
        await scenario(api,worker,selected,stage=stage,cancel_after_exit=cancel,filename=name)

async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=BoundaryWorker,after_publish=boundaries)

if __name__=='__main__':
    asyncio.run(baseline.main(entry='platform/server/live_llm_remote_startup_boundaries_acceptance.py',verifier=verify))
