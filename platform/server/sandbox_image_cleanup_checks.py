"""Actual registered WSL image deletion; no LLM unless a live entry continues."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from sandbox_retirement_checks import verify
from sandbox_image_guards import held


async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[]}
    endpoint='/terminal/sessions/'+sid+'/image-cleanup';body={'recovery_id':rid,'confirm':True}
    def save():
        trace=api.root/'upstream.jsonl'
        if trace.exists():
            rows=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            report['llm_calls']=sum(r['kind']=='http' and r['path'].endswith('/chat/completions') for r in rows)
        (api.root/'image-cleanup-checks.json').write_text(json.dumps(report,indent=2))
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});save();print('IMAGE_CLEANUP_PASS '+name,flush=True)
    def query(sql,args=()):
        with sqlite3.connect(api.root/'test.sqlite') as db:return db.execute(sql,args).fetchall()
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    try:
        snapshot=json.loads(query('SELECT profile_json FROM native_requests WHERE id=?',(rid,))[0][0])
        image_root=Path(snapshot['image_root']);image=image_root/'volume.ext4'
        assert image_root.resolve().parent==(api.root/'files/.terminal-export').resolve()
        archive=Path(snapshot['root'])/'recovery.zip';raw=archive.read_bytes()
        metadata={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in image_root.iterdir() if p.is_file() and p.name!='volume.ext4'}
        assert (await call('GET',endpoint))['status']=='not_started'
        await call('POST',endpoint,body={'recovery_id':rid},expect=422)
        assert image.exists()
        record('explicit_image_deletion_confirmation_required','protocol_fault')
        # Corrupt actual image bytes without replacing its inode; the native
        # gate must verify the full image against the retained snapshot digest.
        with image.open('r+b') as stream:
            stream.seek(-1,2);original=stream.read(1);stream.seek(-1,2);stream.write(bytes([original[0]^1]))
        try:
            await call('POST',endpoint,body=body,expect=409)
            assert image.exists() and image.stat().st_size==128*1024*1024
        finally:
            with image.open('r+b') as stream:stream.seek(-1,2);stream.write(original)
        assert (await call('GET',endpoint))['status']=='pending'
        await call('POST','/terminal/sessions/'+sid+'/recoveries',expect=409)
        await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False,expect=409)
        record('actual_modified_image_rejected_and_new_recovery_fenced','protocol_fault')
        for kind in ('flock','loop'):
            async with held(api,image,kind):
                await call('POST',endpoint,body=body,expect=409)
                assert image.exists() and archive.read_bytes()==raw
            record('actual_'+kind+'_holder_blocks_image_deletion')
        done=await call('POST',endpoint,body=body)
        assert done['status']=='completed' and done['image_absent'] and done['deleted_this_request']
        assert not image.exists() and image_root.exists() and archive.read_bytes()==raw
        assert all(hashlib.sha256((image_root/name).read_bytes()).hexdigest()==sha for name,sha in metadata.items())
        native=json.loads(query('SELECT children_json FROM native_requests WHERE id=?',(done['request_id'],))[0][0])
        children=list(native['children'].values())
        assert len(children)==1 and children[0]['stop']['verified'] and children[0]['purpose']=='sandbox_image_cleanup'
        result=json.loads(query('SELECT result_json FROM native_requests WHERE id=?',(done['request_id'],))[0][0])
        assert result['outer_gate']==children[0]['identity']['native']['process']
        record('registered_outer_gate_removes_only_verified_image_and_records_actual_stop')
        assert await call('POST',endpoint,body=body)==done
        assert await call('GET',endpoint)==done
        await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
        selected=json.loads(query('SELECT profile_json FROM native_sessions WHERE id=?',(sid,))[0][0])
        await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False,expect=409 if selected['staging_prefix'] else 200)
        await call('GET','/terminal/sessions/'+sid+'/recoveries/'+rid)
        await call('GET','/terminal/sessions/'+sid+'/recoveries/'+rid+'/download',raw=True)
        await call('GET',api.prefix+'/raw?path=Ref/retirement.txt',expect=404)
        record('idempotent_completion_preserves_snapshot_download_and_unpublished_project')
        image.write_bytes(b'new unrelated bytes')
        try:
            await call('GET',endpoint,expect=409);await call('POST',endpoint,body=body,expect=409)
            assert image.read_bytes()==b'new unrelated bytes'
        finally:image.unlink()
        record('reappeared_image_is_never_deleted_by_completed_intent','protocol_fault')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_restored_file_after_actual_image_deletion','real_llm')
        report.update(passed=True,receipt=done)
        return report['llm_calls']
    finally:save()


if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    asyncio.run(verify(after_retirement=after_retirement))
