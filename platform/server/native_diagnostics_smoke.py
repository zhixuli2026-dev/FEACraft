"""Diagnostic charging cannot silently reset after file/path substitution."""
import json
import os
from pathlib import Path
import tempfile
import time

from app.services.execution_budget import Budget
from app.services.native_diagnostics import DiagnosticMeter


def verify():
    checks=[]
    with tempfile.TemporaryDirectory(prefix='diagnostic-meter-') as temporary:
        root=Path(temporary)
        for mode in ('growth','truncate','replace','folder_replace','hardlink','directory','disappear'):
            base=root/mode;base.mkdir();folder=base/'call';folder.mkdir()
            budget=Budget('owned',time.monotonic()+30,1024)
            meter=DiagnosticMeter(base,folder/'settings.json',budget,'worker')
            meter.sample()
            path=folder/'stderr.bin';path.write_bytes(b'a'*600);meter.sample();meter.sample()
            assert budget.log_bytes==600
            if mode=='growth':
                with path.open('ab') as stream:stream.write(b'b'*500)
                meter.sample()
                assert budget.log_bytes==1100 and budget.violation=='log_limit'
                assert budget.usage()['observed_pipe_bytes']==0
                checks.append(mode);continue
            if mode=='truncate':path.write_bytes(b'z')
            if mode=='replace':
                path.rename(folder/'prior.bin');path.write_bytes(b'z'*700)
            if mode=='folder_replace':
                folder.rename(base/'prior');folder.mkdir();path.write_bytes(b'z'*700)
            if mode=='hardlink':os.link(path,folder/'alias.bin')
            if mode=='directory':path.unlink();path.mkdir()
            if mode=='disappear':path.unlink()
            try:meter.sample()
            except ValueError:checks.append(mode)
            else:raise AssertionError('Changed diagnostic accepted: '+mode)
            assert budget.log_bytes==600,'Rejected evidence reset prior charges'
    return {'passed':True,'count':len(checks),'checks':checks}


if __name__=='__main__':print('NATIVE_DIAGNOSTICS_OK '+json.dumps(verify()))
