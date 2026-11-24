"""Actual SQLite statement interleaving on a copy of a real CLI turn receipt."""
import argparse,asyncio,hashlib,json,sqlite3,tempfile
from pathlib import Path
from sqlalchemy import select,delete,update
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from testsupport import SERVER

async def verify(source_root,*,before_fix=False):
    from app.models import NativeCliTurn,NativeCliSession,NativeCliEvent
    from app.services.codex_turn_recovery import evidence
    from app.services.workflow_state import ExecutionConflict
    original=Path(source_root).resolve();assert original.is_relative_to(SERVER.parents[1]/'tmp')
    root=Path(tempfile.mkdtemp(prefix='cli-recovery-read-snapshot-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'before_fix':before_fix,'kind':'actual_SQL_on_copy_of_real_CLI_receipt','new_model_calls':0,'mock_llm_calls':0,'original_database_modified':False,'checks':[]}
    original_digest=hashlib.sha256((original/'test.sqlite').read_bytes()).hexdigest()
    with sqlite3.connect((original/'test.sqlite').as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(root/'test.sqlite') as target:source.backup(target)
    engine=create_async_engine('sqlite+aiosqlite:///'+(root/'test.sqlite').as_posix());session=async_sessionmaker(engine,expire_on_commit=False)
    try:
        async with session() as db:
            proof=await db.scalar(select(NativeCliEvent).where(NativeCliEvent.type=='execution.recovered'))
            assert proof is not None
            value=json.loads(proof.payload_json);identifier=proof.turn_id
            turn=await db.get(NativeCliTurn,identifier)
            assert turn.phase=='interrupted' and value['original_turn_phase']=='awaiting_input'
            fields=('phase','version','error','ended_at','next_seq','event_bytes')
            terminal={k:getattr(turn,k) for k in fields}
            appended=(await db.scalars(select(NativeCliEvent).where(NativeCliEvent.turn_id==identifier,NativeCliEvent.type.in_(('stream.done','execution.recovered'))).order_by(NativeCliEvent.seq))).all()
            assert len(appended)==2 and appended[-1].type=='execution.recovered'
            events=[{'turn_id':v.turn_id,'seq':v.seq,'type':v.type,'payload_json':v.payload_json} for v in appended]
            # This isolated SQL precondition recreates the two sides of the
            # already-observed real recovery commit; it runs no model round.
            await db.execute(delete(NativeCliEvent).where(NativeCliEvent.turn_id==identifier,NativeCliEvent.type.in_(('stream.done','execution.recovered'))))
            await db.execute(update(NativeCliTurn).where(NativeCliTurn.id==identifier).values(phase='awaiting_input',version=turn.version-1,error=None,ended_at=None,next_seq=events[0]['seq'],event_bytes=turn.event_bytes-sum(len(v['payload_json'].encode('utf8')) for v in events)))
            await db.commit()
        async with session() as stale:
            row=await stale.get(NativeCliTurn,identifier);scope=await stale.get(NativeCliSession,row.session_id)
            assert row.phase=='awaiting_input'
            async with session() as writer:
                await writer.execute(update(NativeCliTurn).where(NativeCliTurn.id==identifier).values(**terminal))
                writer.add_all([NativeCliEvent(**v) for v in events]);await writer.commit()
            assert row.phase=='awaiting_input','The actual reader identity map was unexpectedly refreshed'
            try:receipt,released=await evidence(stale,row,scope)
            except ExecutionConflict as error:
                assert before_fix and error.code=='evidence_invalid',error.code
                report['checks'].append({'check':'old_code_rejects_real_committed_receipt_with_stale_reader','error_code':error.code})
            else:
                assert not before_fix and receipt==value and released and row.phase=='interrupted'
                report['checks'].append({'check':'single_SQL_snapshot_reads_receipt_and_bound_terminal_together','released':released})
        if not before_fix:
            for label,change in [('binding',{'session_binding':'0'*64}),('owner_exit',{'owner_exit':{'state':'match','reason':'birth_and_scope_match'}}),('legacy_schema',{'schema':'legacy'})]:
                malformed={**value,**change}
                async with session() as writer:
                    await writer.execute(update(NativeCliEvent).where(NativeCliEvent.turn_id==identifier,NativeCliEvent.type=='execution.recovered').values(payload_json=json.dumps(malformed)))
                    await writer.commit()
                async with session() as db:
                    row=await db.get(NativeCliTurn,identifier);scope=await db.get(NativeCliSession,row.session_id)
                    try:await evidence(db,row,scope)
                    except ExecutionConflict as error:assert error.code=='evidence_invalid'
                    else:raise AssertionError('Malformed real receipt accepted: '+label)
                report['checks'].append({'check':'explicit_receipt_protocol_'+label+'_refused','classification':'pure_protocol_fault'})
            async with session() as writer:
                await writer.execute(update(NativeCliEvent).where(NativeCliEvent.turn_id==identifier,NativeCliEvent.type=='execution.recovered').values(payload_json=proof.payload_json))
                writer.add(NativeCliEvent(turn_id=identifier,seq=terminal['next_seq'],type='execution.recovered',payload_json=proof.payload_json));await writer.commit()
            async with session() as db:
                row=await db.get(NativeCliTurn,identifier);scope=await db.get(NativeCliSession,row.session_id)
                try:await evidence(db,row,scope)
                except ExecutionConflict as error:assert error.code=='evidence_invalid'
                else:raise AssertionError('Duplicate receipt accepted')
            report['checks'].append({'check':'explicit_duplicate_receipt_protocol_refused','classification':'pure_protocol_fault'})
        assert hashlib.sha256((original/'test.sqlite').read_bytes()).hexdigest()==original_digest
        report['original_database_sha256_unchanged']=original_digest;report['passed']=True
    finally:
        await engine.dispose()
        report['source_hashes']={p:hashlib.sha256((SERVER/p).read_bytes()).hexdigest() for p in ['cli_recovery_snapshot_acceptance.py','app/services/codex_turn_recovery.py']}
        (root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('CLI_RECOVERY_SNAPSHOT_OK '+str(root),flush=True)
    return root

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source-root',required=True);parser.add_argument('--before-fix',action='store_true');args=parser.parse_args()
    asyncio.run(verify(args.source_root,before_fix=args.before_fix))
