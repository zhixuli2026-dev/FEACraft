"""Run the actual Linux persistent-volume primitive against private test data."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from app.services.owned_process import wsl_path


def main():
    server=Path(__file__).resolve().parent
    evidence=Path(tempfile.mkdtemp(prefix='sandbox-disk-checks-',dir=server.parents[1]/'tmp'))
    root=evidence/'.terminal-export'/uuid.uuid4().hex
    root.mkdir(parents=True)
    (root/'proof.txt').write_text('PERSISTENT_VOLUME_'+uuid.uuid4().hex)
    print('PERSISTENT_VOLUME_EVIDENCE='+str(evidence),flush=True)
    exits={}
    for mode in ('write-and-die','recover'):
        with (evidence/(mode+'.log')).open('wb') as log:
            result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u',
                wsl_path(server/'sandbox_disk_native_checks.py'),wsl_path(root),mode],
                stdout=log,stderr=subprocess.STDOUT,timeout=90)
        exits[mode]=result.returncode
        if mode=='write-and-die':
            assert result.returncode!=0 and (root/'ready.json').exists(),'Writer failed before actual SIGKILL; see '+str(evidence)
        else:assert result.returncode==0,'Recovery failed; see '+str(evidence)
    checks=json.loads((root/'checks.json').read_text())
    checks.update(exits=exits,volume_root=str(root),actual_writer=json.loads((root/'writer.json').read_text()))
    (evidence/'checks.json').write_text(json.dumps(checks,indent=2))
    print('PERSISTENT_VOLUME_CHECKS_OK '+str(evidence/'checks.json'),flush=True)


if __name__=='__main__':main()
