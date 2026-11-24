"""Hold a real flock or owned read-only loop device on an isolated test image."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess

from app.services.owned_process import wsl_path,probe_native

SCRIPT = '''
import fcntl,json,os,subprocess,sys
from pathlib import Path
import importlib.util
image=Path(sys.argv[1]);ready=Path(sys.argv[2]);kind=sys.argv[3]
spec=importlib.util.spec_from_file_location('identity',sys.argv[4]);identity=importlib.util.module_from_spec(spec);spec.loader.exec_module(identity)
device=None;fd=None
try:
 if kind=='flock':
  fd=os.open(image,os.O_RDONLY|os.O_NOFOLLOW);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 elif kind=='loop':
  device=subprocess.check_output(['losetup','--find','--show','--read-only',str(image)],timeout=10).decode().strip()
  assert device.startswith('/dev/loop') and device[9:].isdigit()
 else:raise ValueError('Unknown actual image guard')
 # Publish the handshake only after its bytes are complete. The parent can
 # observe a newly created file before write_text has written any JSON.
 pending=ready.with_suffix('.pending')
 with pending.open('x') as stream:
  stream.write(json.dumps({'owner':identity.observe(os.getpid()),'kind':kind,'device':device}))
  stream.flush();os.fsync(stream.fileno())
 os.rename(pending,ready)
 sys.stdin.readline()
finally:
 if fd is not None:os.close(fd)
 if device:
  rows=json.loads(subprocess.check_output(['losetup','--list','--json','--output','NAME,BACK-FILE'],timeout=10))['loopdevices']
  matches=[r for r in rows if r['name']==device]
  assert len(matches)==1 and Path(matches[0]['back-file']).resolve()==image.resolve()
  subprocess.run(['losetup','--detach',device],check=True,timeout=10)
'''


@asynccontextmanager
async def held(api,image,kind):
    assert image.resolve().is_relative_to((api.root/'files/.terminal-export').resolve()) and image.name=='volume.ext4'
    script=api.root/('image-'+kind+'-guard.py');script.write_text(SCRIPT,encoding='utf8')
    ready=api.root/('image-'+kind+'-guard.json')
    log=(api.root/('image-'+kind+'-guard.log')).open('w',encoding='utf8')
    identity=Path(__file__).parent/'app/services/process_identity_native.py'
    process=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u',wsl_path(script),
        wsl_path(image),wsl_path(ready),kind,wsl_path(identity)],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    owner=None
    try:
        async with asyncio.timeout(20):
            while not ready.exists():
                assert process.poll() is None,'Actual guard exited; inspect '+str(log.name)
                await asyncio.sleep(.02)
        owner=json.loads(ready.read_text())['owner']
        assert (await probe_native('vibeflow-linux',owner))['state']=='match'
        yield
        assert process.poll() is None,'Guard must remain alive throughout the actual refusal'
    finally:
        process.stdin.close()
        await asyncio.to_thread(process.wait,20)
        log.close()
        if owner is not None:
            result=await probe_native('vibeflow-linux',owner)
            (api.root/('image-'+kind+'-guard-exit.json')).write_text(json.dumps(result))
            assert result['state']=='exited'
        assert process.returncode==0
