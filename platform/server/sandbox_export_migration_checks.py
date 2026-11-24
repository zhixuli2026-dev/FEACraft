"""Disposable SQLite schema/rollback checks; SQL protocol only, no inference."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

SERVER=Path(__file__).resolve().parent


def main():
    root=Path(tempfile.mkdtemp(prefix='sandbox-export-migration-',dir=SERVER.parents[1]/'tmp'))
    database=root/'schema.sqlite'
    env=dict(os.environ,VIBEFLOW_DATABASE_URL='sqlite+aiosqlite:///'+database.as_posix(),VIBEFLOW_STORAGE_BACKEND='fs')
    checks=[]
    def command(*arguments,success=True):
        result=subprocess.run([sys.executable,'-B','-m','alembic',*arguments],cwd=SERVER,env=env,
                              capture_output=True,timeout=60)
        with (root/'migration.log').open('ab') as log:log.write(result.stdout+result.stderr)
        assert (result.returncode==0)==success,'Inspect '+str(root/'migration.log')
    command('upgrade','e2c7e93068af')
    command('upgrade','head')
    command('check')
    checks.append('upgrade_to_export_kind_matches_current_metadata')
    command('downgrade','e2c7e93068af')
    command('upgrade','head')
    checks.append('empty_export_history_downgrade_and_reupgrade')
    with sqlite3.connect(database) as db:
        # Isolate the downgrade guard from unrelated parent-row fixtures.
        # This row is schema evidence only, never an execution/cleanup receipt.
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("""INSERT INTO native_requests
            (id,case_id,created_by,owner_id,owner_token_sha256,owner_identity_json,launch_nonce,
             kind,profile_json,heartbeat_at,lease_expires_at,created_at,updated_at)
            VALUES ('schema-only-export',1,1,'schema-only',?, '{}',?,'terminal.export','{}',
                    CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",('a'*64,'b'*64))
    command('downgrade','e2c7e93068af',success=False)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='f3d8fa4179bc'
        assert db.execute("SELECT COUNT(*) FROM native_requests WHERE kind='terminal.export'").fetchone()[0]==1
    checks.append('downgrade_refuses_retained_export_history_without_deleting_it')
    (root/'checks.json').write_text(json.dumps({'passed':True,'checks':checks,'llm_calls':0,
        'classification':'schema_protocol_only','developer_database_untouched':True},indent=2))
    print('SANDBOX_EXPORT_MIGRATION_OK '+str(root/'checks.json'),flush=True)


if __name__=='__main__':main()
