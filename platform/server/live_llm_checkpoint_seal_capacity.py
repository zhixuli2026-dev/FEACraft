"""Measure independent database writes at actual native checkpoint sealing."""
import asyncio
from functools import partial
import json
import os
import sys

from live_llm_job_repeat_acceptance import main
from live_llm_solve_resume_acceptance import TIMING_FIXTURE, scenario


MEASUREMENT=r'''
import sqlite3
from pathlib import Path
from app.services import attempt_workspace
from app.services.solve_resume_artifacts import NAME as checkpoint_name

def observe_checkpoint(manifest, path):
    selected=next((name for name in manifest['files'] if name.endswith('/'+checkpoint_name)),None)
    if selected is None:return
    root=Path(__file__).parent
    sqlite= None
    if (root/'test.sqlite').is_file():
        with sqlite3.connect(root/'test.sqlite',timeout=.2) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                sqlite=True
            except sqlite3.OperationalError as error:
                assert 'locked' in str(error).lower(),str(error)
                sqlite=False
            db.rollback()
    copies=sum((path/side/selected).is_file() for side in ('inputs','run')) if path is not None else 0
    evidence={'attempt':manifest['attempt_id'],'previous':manifest['source']['retry_of_attempt_id'],
              'member_bytes':sum(row['size'] for row in manifest['files'].values()),
              'whole_local_copies':copies,'sqlite_writer':sqlite}
    with (root/'checkpoint-seal-measurements.jsonl').open('a',encoding='utf8') as file:
        file.write(json.dumps(evidence)+'\n')

if Path(__file__).with_name('checkpoint-seal-baseline').exists():
    original_checkpoint_prepare=attempt_workspace.Snapshot.prepare
    def baseline_checkpoint(self):
        manifest=original_checkpoint_prepare(self)
        observe_checkpoint(manifest,self.target)
        return manifest
    attempt_workspace.Snapshot.prepare=baseline_checkpoint
else:
    from app.services import checkpoint_seal, command_input_seal
    local_checkpoint=checkpoint_seal.prepare_files
    remote_checkpoint=command_input_seal.upload_checkpoint
    def measured_local(*args):
        prepared=local_checkpoint(*args)
        observe_checkpoint(prepared.draft,prepared.directory)
        root=Path(__file__).parent
        mutation=root/'checkpoint-actual-mutation.json'
        if (root/'checkpoint-seal-mutate').exists() and not mutation.exists():
            item=args[0]
            source=item.publications[0]
            relative=next(path for path in source.files if path.endswith('/0.005/U'))
            target=(source.root/relative).resolve(strict=True)
            relative=target.relative_to((root/'files').resolve()).as_posix()
            before=target.stat()
            raw=target.read_bytes()
            assert raw and raw[0]!=88
            (root/'checkpoint-original-member.bin').write_bytes(raw)
            target.write_bytes(b'X'+raw[1:])
            os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns))
            after=target.stat()
            assert (after.st_size,after.st_mtime_ns)==(before.st_size,before.st_mtime_ns)
            mutation.write_text(json.dumps({'path':relative,'atime_ns':before.st_atime_ns,'mtime_ns':before.st_mtime_ns,
                'staged_attempt':prepared.id}),encoding='utf8')
        return prepared
    async def measured_remote(lease,owner_id,snapshot,source_guard):
        prepared=await remote_checkpoint(lease,owner_id,snapshot,source_guard)
        observe_checkpoint(prepared.draft['manifest'],snapshot.target)
        return prepared
    checkpoint_seal.prepare_files=measured_local
    command_input_seal.upload_checkpoint=measured_remote
'''


def measure(api, *, baseline, workflow=False, mutate=False, store=None, **kwargs):
    rejected=None
    if mutate:
        original=api.call
        def observed(method,path,*args,**options):
            nonlocal rejected
            candidate=method=='POST' and path.endswith(':repeat') and options.get('expect')==(202,409) and rejected is None
            if candidate:
                jobs={item['id'] for item in original('GET',api.prefix+'/jobs')}
            result=original(method,path,*args,**options)
            mutation=api.root/'checkpoint-actual-mutation.json'
            if not candidate or not mutation.is_file():
                return result
            rejected=json.loads(mutation.read_text(encoding='utf8'))
            target=(api.root/'files'/rejected['path']).resolve(strict=True)
            assert target.is_relative_to((api.root/'files').resolve())
            try:
                assert result['detail']['code']=='input_stale',result
                assert {item['id'] for item in original('GET',api.prefix+'/jobs')}==jobs
                assert not list((api.root/'files/.local-checkpoint-staging').glob('*/attempt'))
            finally:
                target.write_bytes((api.root/'checkpoint-original-member.bin').read_bytes())
                os.utime(target,ns=(rejected['atime_ns'],rejected['mtime_ns']))
            return original(method,path,*args,**options)
        api.call=observed
    result=scenario(api,store=store,repeated_continuation=True,workflow_continuation=workflow,
                    wall_seconds=240 if store else 120,**kwargs)
    path=api.root/'checkpoint-seal-measurements.jsonl'
    samples=[json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]
    assert len(samples)==2+mutate and samples[-2]['attempt']==samples[-1]['previous'],samples
    if mutate:
        assert rejected is not None and rejected['staged_attempt']==samples[0]['attempt']
        assert samples[0]['previous']==samples[1]['previous']
    assert all(row['whole_local_copies']==(2 if baseline or not store else 0) for row in samples),samples
    if api.database_url is None:
        assert all(row['sqlite_writer'] is not baseline for row in samples),samples
    assert result['field_max_abs_differences']=={'U':0.0,'p':0.0}
    report={'passed':True,'baseline':baseline,'workflow':workflow,'samples':samples,'actual_solver':result['actual_solver'],
            'changed_checkpoint_after_staging_rejected':rejected is not None,
            'field_max_abs_differences':result['field_max_abs_differences']}
    (api.root/'checkpoint-seal-capacity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


if __name__=='__main__':
    from s3testsupport import ObjectStore, SERVER

    baseline='--baseline' in sys.argv
    if baseline:
        assert '--s3' not in sys.argv
        sys.argv.remove('--baseline')
    workflow='--workflow' in sys.argv
    if workflow:
        assert not baseline
        sys.argv.remove('--workflow')
    mutate='--mutate-on-stage' in sys.argv
    if mutate:
        assert not baseline and not workflow and '--s3' not in sys.argv
        sys.argv.remove('--mutate-on-stage')
    fixture=TIMING_FIXTURE
    if baseline:fixture+="\n(root/'checkpoint-seal-baseline').touch()\n"
    if mutate:fixture+="\n(root/'checkpoint-seal-mutate').touch()\n"
    fixture+=MEASUREMENT
    binary=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(measure,baseline=baseline,workflow=workflow,mutate=mutate),fixture_override=fixture,store_volume_max=16,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=16,authenticated=True)))
