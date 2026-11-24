"""Disposable SQLite schema/rollback evidence only; no simulated inference."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

def main():
    server=Path(__file__).resolve().parent
    root=Path(tempfile.mkdtemp(prefix='sandbox-recovery-migration-',dir=server.parents[1]/'tmp'))
    database=root/'schema.sqlite'
    env=dict(os.environ,VIBEFLOW_DATABASE_URL='sqlite+aiosqlite:///'+database.as_posix(),VIBEFLOW_STORAGE_BACKEND='fs')
    checks=[]
    def command(*args,success=True):
        result=subprocess.run([sys.executable,'-B','-m','alembic',*args],cwd=server,env=env,capture_output=True,timeout=60)
        with (root/'migration.log').open('ab') as log:log.write(result.stdout+result.stderr)
        assert (result.returncode==0)==success,'Inspect '+str(root/'migration.log')
    command('upgrade','f3d8fa4179bc')
    command('upgrade','head');command('check')
    checks.append('upgrade_adds_recovery_kind_and_result_receipt_matching_metadata')
    command('downgrade','f3d8fa4179bc');command('upgrade','head')
    checks.append('empty_history_downgrade_and_reupgrade')
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("""INSERT INTO native_requests
          (id,case_id,created_by,owner_id,owner_token_sha256,owner_identity_json,launch_nonce,kind,
           profile_json,heartbeat_at,lease_expires_at,created_at,updated_at)
          VALUES ('schema-recovery-only',1,1,'schema-only',?,'{}',?,'terminal.recover','{}',
          CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",('a'*64,'b'*64))
    command('downgrade','f3d8fa4179bc',success=False)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='a4e9bc5280cd'
        assert db.execute("SELECT COUNT(*) FROM native_requests WHERE kind='terminal.recover'").fetchone()[0]==1
    checks.append('retained_recovery_history_refuses_destructive_downgrade')
    (root/'checks.json').write_text(json.dumps({'passed':True,'checks':checks,'llm_calls':0,'mock_llm_calls':0,
        'classification':'schema_protocol_only','developer_database_untouched':True},indent=2))
    print('RECOVERY_MIGRATION_EVIDENCE='+str(root/'checks.json'),flush=True)

if __name__=='__main__':main()
