"""Real PPT/PDF/FloTHERM workers interrupted at explicit fixture checkpoints.

A test wrapper pauses after an actual library/generator call returns. This
tests lifecycle cleanup, not uninstrumented runtime or atomic publication.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from pypdf import PdfWriter
from app.services.flotherm_ir import build_ir
from app.services import process_identity_native as identity
from professional_launch_smoke import settled
from professional_stop_smoke import CONTROLLED, members, wait_for
from testsupport import IsolatedServer, SERVER


WRAPPER='''import hashlib,json,os,runpy,sys,threading
from pathlib import Path
sys.path.insert(0,{server!r})
from app.services import process_identity_native as identity
capture=Path(sys.argv[1]);worker=Path(sys.argv[2]);sys.argv=sys.argv[2:]
sys.path.insert(0,str(worker.parent))
targets={{'ppt_worker.py':('shapetree.py','add_table'),
         'pdf_number_worker.py':('pdf_pages.py','_page_number_stream'),
         'flotherm_convert_job.py':('flotherm_case.py','generate_case'),
         'flotherm_convert_worker.py':('flotherm_case.py','generate_case')}}
target=targets[worker.name]
def checkpoint(frame,event,value):
    if event!='return' or (Path(frame.f_code.co_filename).name,frame.f_code.co_name)!=target:return
    sys.setprofile(None)
    if worker.name=='ppt_worker.py':completed={{'table_rows':len(value.table.rows),'table_columns':len(value.table.columns)}}
    elif worker.name=='pdf_number_worker.py':completed={{'page_number':frame.f_locals['page_number'],'page_count':frame.f_locals['page_count'],'stream_bytes':len(value)}}
    else:completed={{'objects':len(value['objects']),'application':value['application']}}
    capture.write_text(json.dumps({{'identity':identity.observe(os.getpid()),'worker':worker.name,
        'worker_sha256':hashlib.sha256(worker.read_bytes()).hexdigest(),'checkpoint':list(target),'completed':completed}}))
    # Intentional bounded fault pause, never represented as native CPU work.
    threading.Event().wait(60)
    raise TimeoutError('Fixture checkpoint was not interrupted within 60 seconds')
sys.setprofile(checkpoint)
runpy.run_path(str(worker),run_name='__main__')
'''

INSTRUMENT='''
before_fixture=jobrunner.JobProcess.__init__
def instrument(self,*args,**kwargs):
    argv=kwargs.get('argv')
    if argv and Path(argv[1]).name in {'ppt_worker.py','pdf_number_worker.py','flotherm_convert_job.py','flotherm_convert_worker.py'}:
        capture=root/((root/'mode').read_text()+'-checkpoint.json')
        kwargs['argv']=[argv[0],str(root/'observe_worker.py'),str(capture),*argv[1:]]
    before_fixture(self,*args,**kwargs)
jobrunner.JobProcess.__init__=instrument
'''

FLOTHERM_BUDGET = '''
from app.services.flotherm_convert_handler import FlothermConvertHandler
prepare_flotherm=FlothermConvertHandler.prepare

async def bounded_flotherm(self,*args,**kwargs):
    if (root/'mode').read_text()=='timeout':
        kwargs['budget']={**kwargs['budget'],'timeout_seconds':18}
    return await prepare_flotherm(self,*args,**kwargs)

FlothermConvertHandler.prepare=bounded_flotherm
'''


def prepare(api,backend,directory):
    if backend=='ppt':
        return '/ppt:build',{'output':'output.pptx','slides':[{'title':'Actual table','table':{
            'headers':['A','B'],'rows':[['1','2'],['3','4']]}}]},[]
    if backend=='pdf':
        source=directory/'input.pdf'
        pdf=PdfWriter()
        for _ in range(3):pdf.add_blank_page(width=300,height=400)
        with source.open('xb') as stream:pdf.write(stream)
        return '/pdf:number',{'input':'input.pdf','output':'output.pdf'},[source]
    ir=build_ir('fixture.ecxml',b'fixture',[{'id':'chip','type':'box','material':'silicon',
        'mapping':'converted','power_w':5,'shape':{'kind':'box','origin':[0,0,0],'dimensions':[1,1,1]}}],
        {'silicon':{'conductivity_w_mk':130,'rho_cp':1000}})
    api.put('ir.json',json.dumps(ir))
    return '/flotherm:convert',{'ir_path':'ir.json','output_dir':'conversion-output'},[directory/'ir.json']


def verify(backend):
    api=IsolatedServer('worker-scope-'+backend,app='controlled_worker:app')
    fixture = CONTROLLED+INSTRUMENT
    if backend == 'flotherm':
        fixture = fixture.replace("kwargs['timeout_seconds']=8", "kwargs['timeout_seconds']=18") + FLOTHERM_BUDGET
    (api.root/'controlled_worker.py').write_text(fixture,encoding='utf8')
    (api.root/'observe_worker.py').write_text(WRAPPER.format(server=str(SERVER)),encoding='utf8')
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
                api.create_case(backend+' worker '+mode)
                directory=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                endpoint,body,inputs=prepare(api,backend,directory)
                preserved=directory/'unrelated.txt';preserved.write_bytes(b'Fixture unrelated case file')
                inputs.append(preserved)
                hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
                job_id=api.call('POST',api.prefix+endpoint,body)['job_id']
                checkpoint=api.root/(mode+'-checkpoint.json')
                wait_for(lambda:checkpoint.exists(), seconds=25)
                captured=json.loads(checkpoint.read_text())
                assert hashlib.sha256((SERVER/'app/services'/captured['worker']).read_bytes()).hexdigest()==captured['worker_sha256']
                if backend=='ppt':assert captured['completed']=={'table_rows':3,'table_columns':2}
                elif backend=='pdf':assert captured['completed']['page_count']==3 and captured['completed']['stream_bytes']>0
                else:
                    assert captured['completed']=={'objects':1,'application':'laplacianFoam'}
                    checkpoint_job=api.call('GET',f'/jobs/{job_id}')
                    private=directory/checkpoint_job['attempt']['workspace']['run_path']
                    assert (private/'conversion-output/system/blockMeshDict').is_file()
                    assert not (directory/'conversion-output').exists()
                row=api.call('GET',f'/jobs/{job_id}')
                record=row['attempt']['process_identity']
                scope_members=members(record['scope'])
                assert captured['identity'] in [m['identity'] for m in scope_members]
                assert all(identity.probe(m['identity'])['state']=='match' for m in scope_members)
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
                    assert stop['reason']==('cancelled' if mode=='cancel' else 'timeout'), stop
                wait_for(lambda:all(identity.probe(m['identity'])['state']=='exited' for m in scope_members))
                assert sentinel.poll() is None
                assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest for path,digest in hashes.items())
                output='output.pptx' if backend=='ppt' else 'output.pdf' if backend=='pdf' else 'conversion-output/flotherm-ir.json'
                assert not (directory/output).exists()
                if backend == 'flotherm':
                    assert (private/'conversion-output/system/blockMeshDict').is_file()
                receipts.append({'mode':mode,'job_id':job_id,'checkpoint':captured,'members':scope_members,
                    'final':final,'preserved_sha256':hashes,'external_sentinel_alive':True})
                checks.append(backend+'_'+mode+'_after_real_work_checkpoint_scope_exits')
                (api.root/'scope-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    finally:
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),'backend':backend,
            'real_worker_instrumented_with_checkpoint':True,'atomic_publication_not_claimed':True}
    (api.root/'scope-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


def main():
    root=Path(tempfile.mkdtemp(prefix='professional-worker-evidence-',dir=SERVER.parents[1]/'tmp'))
    result={backend:verify(backend) for backend in ('ppt','pdf','flotherm')}
    (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':sum(r['count'] for r in result.values()),'evidence':str(root)}))


if __name__=='__main__':main()
