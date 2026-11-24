"""Actual process death during bundle preparation, followed by journal recovery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

from testsupport import SERVER


def child(root,mode):
    os.environ['VIBEFLOW_STORAGE_BACKEND']='fs'
    os.environ['VIBEFLOW_STORAGE_FS_ROOT']=str(root/'files')
    from app.services import artifact_publication as pub, publication_journal as journal
    from app.services import attempt_workspace as workspace
    info=json.loads((root/'context.json').read_text())
    row=SimpleNamespace(**info['row']);run=root/'run'
    records=workspace.inventory(run,['a.txt','nested'])
    if mode=='partial_copy':
        def partial(source,target,records,directories):
            target.mkdir();(target/'a.txt').write_text('partial');os._exit(41)
        workspace.copy_records=partial
    if mode in {'before_rename','after_rename'}:
        move=pub.move_new
        def moving(source,target,**kwargs):
            if mode=='before_rename':os._exit(41)
            move(source,target,**kwargs);os._exit(41)
        pub.move_new=moving
    if mode=='after_ready':
        ready=journal._ready
        def writing(*args):ready(*args);os._exit(41)
        journal._ready=writing
    if mode=='hold':
        root_path,directory=journal._directory('case',row)
        with journal._lock(directory):
            (root/'lock-held').touch()
            while not (root/'release').exists():time.sleep(.02)
        return
    if mode=='inspect':
        result=journal.inspect('case',row)
    else:
        digest=pub.prepare_bundle('case',row,run,records,info['descriptor'])
        result={'sha256':digest,'diagnosis':journal.inspect('case',row)}
    (root/('result-'+mode+'.json')).write_text(json.dumps(result),encoding='utf8')


def launch(root,mode,expected=0):
    result=subprocess.run([sys.executable,'-X','utf8','-B',__file__,'--child',str(root),'--mode',mode],
        cwd=SERVER,capture_output=True,text=True,encoding='utf8',timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    (root/(mode+'.log')).write_text(result.stdout+result.stderr,encoding='utf8')
    assert result.returncode==expected,(mode,result.returncode,result.stderr)


def fixture(report,name):
    root=report/name;root.mkdir();(root/'files/case').mkdir(parents=True)
    (root/'run/nested').mkdir(parents=True)
    (root/'run/a.txt').write_bytes(b'A'*8192)
    (root/'run/nested/b.txt').write_bytes('完整产物\n'.encode('utf8')*200)
    publication_id=str(uuid.uuid4())
    row={'id':publication_id,'case_id':17,'attempt_id':str(uuid.uuid4()),'kind':'report','generation':1,
        'immutable_path':'.vibeflow/publications/'+publication_id,'manifest_sha256':'0'*64,'status':'prepared'}
    context={'row':row,'descriptor':{'handler':'fixture.v1','input':'fixed'}}
    (root/'context.json').write_text(json.dumps(context),encoding='utf8')
    return root,context


def verify():
    report=Path(tempfile.mkdtemp(prefix='publication-journal-evidence-',dir=SERVER.parents[1]/'tmp'))
    checks=[]
    for mode in ('partial_copy','before_rename','after_rename','after_ready'):
        root,context=fixture(report,mode)
        launch(root,mode,41)
        launch(root,'inspect')
        diagnosis=json.loads((root/'result-inspect.json').read_text())
        assert diagnosis['state']==('bundle_verified' if mode in {'after_rename','after_ready'} else 'copy_incomplete'),diagnosis
        target=root/'files/case'/context['row']['immutable_path']
        manifest_before=(target/'manifest.json').read_bytes() if target.exists() else None
        launch(root,'recover')
        first=json.loads((root/'result-recover.json').read_text())
        assert first['diagnosis']['state']=='bundle_verified',first
        before={p.relative_to(target).as_posix():(p.read_bytes(),p.stat().st_mtime_ns) for p in target.rglob('*') if p.is_file()}
        launch(root,'repeat')
        after={p.relative_to(target).as_posix():(p.read_bytes(),p.stat().st_mtime_ns) for p in target.rglob('*') if p.is_file()}
        assert before==after,'Repeated preparation rewrote immutable output'
        if manifest_before is not None:assert (target/'manifest.json').read_bytes()==manifest_before
        if mode=='partial_copy':
            quarantine=list((root/'files/case/.vibeflow/publications/.journal'/context['row']['id']).glob('interrupted-*'))
            assert len(quarantine)==1 and (quarantine[0]/'files/a.txt').read_text()=='partial'
        checks.append(mode+'_real_process_death_and_idempotent_reuse')
    root,context=fixture(report,'corruption');launch(root,'initial')
    target=root/'files/case'/context['row']['immutable_path']
    (target/'files/a.txt').write_bytes(b'changed')
    launch(root,'inspect');assert json.loads((root/'result-inspect.json').read_text())['state']=='requires_attention'
    launch(root,'reject',1);assert (target/'files/a.txt').read_bytes()==b'changed'
    checks.append('damaged_existing_bundle_preserved_and_rejected')
    root,context=fixture(report,'changed_intent');launch(root,'initial')
    context['descriptor']['input']='different';(root/'context.json').write_text(json.dumps(context))
    launch(root,'reject',1);checks.append('changed_intent_rejected')
    root,context=fixture(report,'process_lock');launch(root,'initial')
    proc=subprocess.Popen([sys.executable,'-X','utf8','-B',__file__,'--child',str(root),'--mode','hold'],
        cwd=SERVER,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline=time.monotonic()+10
        while not (root/'lock-held').exists():
            assert proc.poll() is None and time.monotonic()<deadline
            time.sleep(.02)
        launch(root,'locked',1)
    finally:
        (root/'release').touch();proc.wait(timeout=10)
    launch(root,'released');checks.append('concurrent_process_cannot_take_preparation_lock')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(report)}
    (report/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('PUBLICATION_JOURNAL_OK '+json.dumps(result),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--child',type=Path);parser.add_argument('--mode');args=parser.parse_args()
    child(args.child,args.mode) if args.child else verify()
