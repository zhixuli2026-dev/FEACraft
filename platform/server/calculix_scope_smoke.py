"""Interrupt the actual CalculiX solver while a bounded multi-step deck runs."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from testsupport import IsolatedServer
from professional_launch_smoke import calculix_deck, settled
from professional_stop_smoke import CONTROLLED, members, wait_for
from app.services import process_identity_native as identity


def long_deck():
    # Keep the accepted eight-node physical model, repeat actual static steps.
    # No replacement solver or sleeping executable; < 1 MiB HTTP input limit.
    source=calculix_deck().split('*STEP\n',1)[0]
    source+='*STEP\n*STATIC\n*CLOAD\n5,3,-1000\n*END STEP\n'*20000
    assert len(source.encode())<1024*1024
    return source


def main():
    api=IsolatedServer('calculix-scope',app='controlled_calculix:app')
    (api.root/'controlled_calculix.py').write_text(CONTROLLED,encoding='utf8')
    (api.root/'mode').write_text('cancel')
    sentinel=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-I','-S','-u','-c',
        'import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    checks=[];receipts=[]
    try:
        with api:
            for mode in ('cancel','timeout','api_death'):
                (api.root/'mode').write_text(mode)
                api.create_case('CalculiX interruption '+mode)
                api.put('C-run/long.inp',long_deck())
                directory=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                source=directory/'C-run/long.inp'
                digest=hashlib.sha256(source.read_bytes()).hexdigest()
                job_id=api.call('POST',api.prefix+'/calculix:solve',{'input_path':'C-run/long.inp'})['job_id']
                row=api.call('GET',f'/jobs/{job_id}')
                record=row['attempt']['process_identity']
                progress=directory/row['attempt']['workspace']['run_path']/'C-run/long.sta'
                def solver_members():
                    children=api.call('GET',f'/jobs/{job_id}')['attempt']['child_processes']['children']
                    running=[c for c in children.values() if c['phase']=='running' and c['identity']]
                    if not running:return None
                    current=members(running[0]['identity']['scope'])
                    # More than just the STA header: actual increment records.
                    if not progress.exists() or len(progress.read_text(errors='replace').splitlines())<4:
                        return None
                    return current if any('ccx' in Path(m['executable']).name.lower() for m in current) else None
                captured=wait_for(solver_members,seconds=7)
                observed=progress.read_bytes()
                (api.root/(mode+'-progress.sta')).write_bytes(observed)
                assert all(identity.probe(m['identity'])['state']=='match' for m in captured)
                if mode=='api_death':
                    owner=json.loads((api.root/'api-identity.json').read_text())
                    assert identity.probe(owner)['state']=='match'
                    (api.root/'exit-owned-api').touch()
                    wait_for(lambda:identity.probe(owner)['state']=='exited')
                    final=None
                else:
                    if mode=='cancel':assert api.call('POST',f'/jobs/{job_id}:cancel')['ok']
                    final=settled(api,job_id)
                    assert final['status']==('cancelled' if mode=='cancel' else 'failed'),final
                    stop=final['attempt']['process_stop']
                    assert stop['verified'] and stop['scope_empty'] and stop['scope_observation']['complete']
                    assert stop['reason']==('cancelled' if mode=='cancel' else 'timeout')
                    children=final['attempt']['child_processes']['children']
                    assert children and all(c['stop'] and c['stop']['verified'] for c in children.values())
                    assert not (directory/'C-run/long.sta').exists(),'Interrupted output leaked into live case'
                wait_for(lambda:all(identity.probe(m['identity'])['state']=='exited' for m in captured))
                assert sentinel.poll() is None and hashlib.sha256(source.read_bytes()).hexdigest()==digest
                receipts.append({'mode':mode,'job_id':job_id,'identity':record,'members':captured,'final':final,
                    'progress_sha256':hashlib.sha256(observed).hexdigest(),'source_sha256':digest,
                    'external_sentinel_alive':True})
                checks.append('actual_calculix_'+mode+'_after_static_increments_exits')
                (api.root/'scope-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    finally:
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'actual_solver':'CalculiX','bounded_input_steps':20000,'api_death_has_no_fabricated_terminal_receipt':True}
    (api.root/'scope-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':main()
