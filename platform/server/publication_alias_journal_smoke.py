"""File-level replay, edit conflicts, retirement and tamper rejection."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

from testsupport import SERVER


def main():
    report=Path(tempfile.mkdtemp(prefix='alias-plan-evidence-',dir=SERVER.parents[1]/'tmp'))
    os.environ['VIBEFLOW_STORAGE_BACKEND']='fs';os.environ['VIBEFLOW_STORAGE_FS_ROOT']=str(report/'files')
    from app import storage
    from app.services import artifact_publication as pub,attempt_workspace as workspace,publication_alias_journal as aliases
    from app.services.compose_artifacts import record
    checks=[]
    def setup(name,retired=None,handler='fixture.v1'):
        prefix=name;root=report/'files'/name;root.mkdir(parents=True)
        run=report/('run-'+name);run.mkdir();(run/'a.txt').write_bytes(b'new A');(run/'b.txt').write_bytes(b'new B')
        identifier=str(uuid.uuid4())
        row=SimpleNamespace(id=identifier,case_id=1,attempt_id=str(uuid.uuid4()),kind='report',generation=1,
            immutable_path='.vibeflow/publications/'+identifier,manifest_sha256='0'*64,status='committed')
        descriptor={'handler':handler,'retired_aliases':retired or {}}
        row.manifest_sha256=pub.prepare_bundle(prefix,row,run,workspace.inventory(run,['a.txt','b.txt']),descriptor)
        _,manifest=pub.read_manifest(prefix,row)
        return prefix,root,row,manifest
    prefix,root,row,manifest=setup('partial',{'old.txt':record(b'old')})
    (root/'old.txt').write_bytes(b'old');(root/'a.txt').write_bytes(b'old A')
    digest=aliases.prepare(prefix,row,manifest)
    put=storage._fs_put
    def fail_after_one(*args,**kwargs):
        put(*args,**kwargs)
        raise OSError('injected after atomic file write')
    storage._fs_put=fail_after_one
    try:
        try:aliases.apply(prefix,row,digest)
        except OSError:pass
        else:raise AssertionError('File fault did not fire')
    finally:storage._fs_put=put
    assert not (root/'old.txt').exists() and (root/'a.txt').read_bytes()==b'new A'
    stamp=(root/'a.txt').stat().st_mtime_ns
    aliases.apply(prefix,row,digest)
    assert (root/'b.txt').read_bytes()==b'new B' and (root/'a.txt').stat().st_mtime_ns==stamp
    stamps={p.name:p.stat().st_mtime_ns for p in root.glob('*.txt')}
    aliases.apply(prefix,row,digest)
    assert stamps=={p.name:p.stat().st_mtime_ns for p in root.glob('*.txt')}
    checks.append('partial_write_and_retirement_resume_without_rewriting_completed_files')
    prefix,root,row,manifest=setup('edited')
    digest=aliases.prepare(prefix,row,manifest);(root/'b.txt').write_bytes(b'USER EDIT')
    try:aliases.apply(prefix,row,digest)
    except ValueError:pass
    else:raise AssertionError('Edited destination overwritten')
    assert not (root/'a.txt').exists() and (root/'b.txt').read_bytes()==b'USER EDIT'
    checks.append('preflight_rejects_all_writes_if_one_destination_was_edited')
    prefix,root,row,manifest=setup('retired_edit',{'old.txt':record(b'old')})
    (root/'old.txt').write_bytes(b'USER RETAINED')
    digest=aliases.prepare(prefix,row,manifest)
    try:aliases.apply(prefix,row,digest)
    except ValueError:pass
    else:raise AssertionError('Edited retirement baseline was blessed')
    assert (root/'old.txt').read_bytes()==b'USER RETAINED' and not (root/'a.txt').exists()
    checks.append('retirement_keeps_original_hash_condition_and_preserves_user_changes')
    for timing in ('before_prepare','after_prepare','current_conflict'):
        prefix,root,row,manifest=setup('solve_'+timing,
            {'old.txt':record(b'old'),'obsolete.txt':record(b'obsolete')},'solve.v1')
        (root/'old.txt').write_bytes(b'USER RETAINED' if timing=='before_prepare' else b'old')
        (root/'obsolete.txt').write_bytes(b'obsolete')
        digest=aliases.prepare(prefix,row,manifest)
        (root/'old.txt').write_bytes(b'USER RETAINED')
        if timing=='current_conflict':(root/'b.txt').write_bytes(b'CURRENT EDIT')
        receipt=root/'.vibeflow/publications/.journal'/row.id/'aliases-applied.json'
        for _ in range(2):
            try:aliases.apply(prefix,row,digest)
            except ValueError:pass
            else:raise AssertionError('Preserved solve edit lost its repair error')
            assert (root/'old.txt').read_bytes()==b'USER RETAINED' and not receipt.exists()
        if timing=='current_conflict':
            assert not (root/'a.txt').exists() and (root/'obsolete.txt').read_bytes()==b'obsolete'
            assert (root/'b.txt').read_bytes()==b'CURRENT EDIT'
        else:
            assert not (root/'obsolete.txt').exists()
            assert (root/'a.txt').read_bytes()==b'new A' and (root/'b.txt').read_bytes()==b'new B'
            (root/'old.txt').unlink()
            aliases.apply(prefix,row,digest)
            assert receipt.exists()
        checks.append('solve_retirement_'+timing+'_preserves_edits_and_replay_conditions')
    prefix,root,row,manifest=setup('tamper');digest=aliases.prepare(prefix,row,manifest)
    path=root/'.vibeflow/publications/.journal'/row.id/'aliases.json'
    value=json.loads(path.read_text());value['operations'][0]['path']='unexpected.txt'
    path.write_text(json.dumps(value))
    try:aliases.apply(prefix,row,digest)
    except ValueError:pass
    else:raise AssertionError('Changed plan accepted')
    assert not list(root.glob('*.txt'))
    checks.append('changed_plan_rejected_against_committed_digest')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(report)}
    (report/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('ALIAS_PLAN_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
