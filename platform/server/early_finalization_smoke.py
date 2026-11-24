"""Original real LaTeX fault cases plus failure outcome and unresolved-drain assertions."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import uuid

from native_execution_smoke import verify
from testsupport import SERVER
from workflow_migration_smoke import alembic


def inspect_rows(rows,events):
    checks=[]
    for job in rows:
        assert job['phase']==job['status'] and job['status'] in {'failed','interrupted'}
        assert job['ended_at'] and job['write_checkpoint_json']=='{}'
        assert job['publication_status']=='failed' and job['active_publication_id'] is None
        output=json.loads(job['output_manifest_json'])
        assert output['publication']['status']=='failed' and 'finalization_intent' not in output
        notices=[json.loads(e['payload_json']) for e in events if e['attempt_id']==job['attempt_id'] and e['type']=='job.recovery_required']
        assert notices and notices[-1]['reason']=='process_cleanup_unverified'
        assert notices[-1]['stop_evidence_created'] is False and notices[-1]['replayed'] is False
        assert notices[-1]['next_step']=='verify_process_cleanup'
        # Outcome is settled, but unknown process drainage is not manufactured.
        children=json.loads(job['child_processes_json'])['children']
        assert any(c['phase']=='running' and c['stop'] is None for c in children.values())
        if job['status']=='interrupted':assert 'process_stop' not in output
        checks.append(job['status']+'_visible_without_fabricated_cleanup_or_publication')
    assert checks
    return checks


QUERY='''SELECT j.status,j.ended_at,a.id AS attempt_id,a.phase,a.write_checkpoint_json,
a.output_manifest_json,a.child_processes_json,p.status AS publication_status,h.active_publication_id
FROM jobs j JOIN job_attempts a ON a.job_id=j.id
JOIN artifact_publications p ON p.attempt_id=a.id
LEFT JOIN artifact_publication_heads h ON h.case_id=j.case_id AND h.kind=p.kind
ORDER BY j.id'''


def verify_database(database_url=None):
    first=verify(database_url,('finalizer_receipt_fail',))
    last=verify(database_url,('finalizer_owner_death',))
    extra=[]
    if database_url is None:
        for result in (first,last):
            with sqlite3.connect(Path(result['evidence'])/'test.sqlite') as db:
                db.row_factory=sqlite3.Row
                extra.extend(inspect_rows([dict(r) for r in db.execute(QUERY)],
                    [dict(r) for r in db.execute('SELECT attempt_id,type,payload_json FROM durable_events ORDER BY seq')]))
    return {'passed':True,'original_scenarios':2,'runs':[first,last],'checks':extra}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='early-finalization-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify_database)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_earlyfinalization_'+uuid.uuid4().hex
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify_database,temporary)
            check=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database=name,timeout=10)
            try:
                results['postgresql']['checks']=inspect_rows(await check.fetch(QUERY),await check.fetch('SELECT attempt_id,type,payload_json FROM durable_events ORDER BY seq'))
            finally:await check.close()
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('EARLY_FINALIZATION_OK '+str(report))


if __name__=='__main__':asyncio.run(main())
