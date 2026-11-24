"""Real process death before/after local journal link installation."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid

from testsupport import SERVER

PREFIX = 'workspaces/1/cases/1'


def child(mode, identifier):
    from app.services import attempt_seal_journal as seals, file_transactions as journal
    original_write, original_link = journal._write_new, os.link
    def hold():
        print('READY', flush=True)
        sys.stdin.read()
        raise RuntimeError('Fixture unexpectedly released')
    def writing(path, data):
        result = original_write(path, data)
        if '/writing/' in path.as_posix() and ((mode == 'intent' and path.suffix == '.json') or
                                               (mode == 'data' and path.suffix == '.part')):
            hold()
        return result
    def linking(source, target):
        result = original_link(source, target)
        if '/attempt-provisions/' in str(target).replace('\\', '/'):
            hold()
        return result
    journal._write_new, os.link = writing, linking
    seals.put(PREFIX, seals.PREFIX + '/pending/' + identifier + '.json', b'{"fixture":"complete"}')


def main():
    report = Path(tempfile.mkdtemp(prefix='plan-write-evidence-', dir=SERVER.parents[1] / 'tmp'))
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(report / 'files'), VIBEFLOW_STORAGE_BACKEND='fs')
    from app.config import get_settings
    get_settings.cache_clear()
    from app.services import attempt_plan_writes as writes, attempt_seal_journal as seals
    from app.services.fs_directory import remove_owned
    checks = []
    for mode in ['intent', 'data', 'link', 'changed', 'extra-link', 'foreign-owner', 'missing-part']:
        identifier = str(uuid.uuid4())
        child_mode = mode if mode in {'intent', 'data'} else 'link'
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', __file__, '--child', child_mode, identifier],
                                   cwd=SERVER, env=os.environ.copy(), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf8')
        try:
            assert process.stdout.readline().strip() == 'READY', process.stderr.read()
            writing = seals.local(PREFIX, seals.PREFIX + '/writing')
            intent = next(writing.glob('*.json'))
            record = json.loads(intent.read_text())
            part = intent.with_suffix('.part')
            target = report / 'files' / PREFIX / record['target']
            assert writes.recover(PREFIX)[0]['status'] == 'active_writer'
            os.kill(record['owner']['pid'], signal.SIGTERM)
            process.wait(timeout=10)
            if mode == 'changed':
                part.write_bytes(b'CHANGED')
            elif mode == 'extra-link':
                os.link(part, report / 'extra-link')
            elif mode == 'foreign-owner':
                record['owner']['host_id'] = '0' * 64
                intent.write_text(json.dumps(record))
            elif mode == 'missing-part':
                part.unlink()
            before = target.read_bytes() if target.exists() else None
            diagnosed = writes.recover(PREFIX, apply=False)[0]
            protected = mode in {'changed', 'extra-link', 'foreign-owner'}
            assert diagnosed['status'] == ('unverified_writer_retained' if mode == 'foreign-owner' else
                                            'recovery_required' if protected else 'journal_write_pending'), diagnosed
            result = writes.recover(PREFIX)[0]
            if protected:
                assert result['status'] in {'unverified_writer_retained', 'recovery_required'}
                assert intent.exists() and part.exists()
            else:
                assert result['status'] == 'journal_write_removed', result
                assert not intent.exists() and not part.exists()
                assert writes.recover(PREFIX) == []
                if target.exists():
                    assert target.stat().st_nlink == 1
                    assert seals.read(PREFIX, record['target'])[0] == before
            assert (target.read_bytes() if target.exists() else None) == before
            checks.append(mode)
        finally:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close(); process.stderr.close()
            (report / 'extra-link').unlink(missing_ok=True)
            root = report / 'files' / PREFIX
            if root.exists():
                remove_owned(root, root=report)
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'evidence': str(report)}
    (report / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result), flush=True)


def api_check(history=False):
    from attempt_seal_recovery_smoke import boot
    from case_provision_smoke import crash
    from case_constructor_recovery_smoke import read_plans
    with boot({}) as first:
        case = first.create_case('Writing recovery')
        owner = first.call('GET', '/auth/me')['id']
        prefix = read_plans(first, None, owner)[0]['binding']['prefix']
        first.put('Ref/input.txt', 'INPUT')
        if history:
            first.call('POST', first.prefix + '/jobs', {'command': 'echo COMMITTED_WRITE'})
    # Install a fault in a new API process sharing only this disposable fixture.
    shared = {key: first.env[key] for key in ('VIBEFLOW_DATABASE_URL', 'VIBEFLOW_STORAGE_FS_ROOT',
                                             'VIBEFLOW_INDEX_ROOT', 'VIBEFLOW_JWT_SECRET')}
    fault = boot(shared)
    fixture = fault.root / 'seal_fixture.py'
    fault_code = '''
import os
link=os.link
def linking(source,target):
 result=link(source,target)
 if '/attempt-provisions/pending/' in str(target).replace(chr(92),'/'):hold()
 return result
os.link=linking
'''
    if history:
        fault_code = fault_code.replace('/attempt-provisions/pending/', '/attempt-provisions/history/')
    fixture.write_text(fixture.read_text(encoding='utf8') + fault_code, encoding='utf8')
    with fault:
        fault.token = first.token
        if history:
            crash(fault, lambda: fault.call('POST', f'/cases/{case["id"]}/attempt-preparations:recover'))
        else:
            crash(fault, lambda: fault.call('POST', f'/cases/{case["id"]}/jobs', {'command': 'echo WRITE_RECOVERY'}))
    with boot(shared, fixture=False) as peer:
        peer.token = first.token
        endpoint = f'/cases/{case["id"]}/attempt-preparations'
        diagnosed = peer.call('GET', endpoint)['results']
        assert [r['status'] for r in diagnosed] == ['journal_write_pending', 'committed' if history else 'recovery_required'], diagnosed
        result = peer.call('POST', endpoint + ':recover')['results']
        assert [r['status'] for r in result] == ['journal_write_removed', 'committed' if history else 'rolled_back'], result
        assert peer.call('POST', endpoint + ':recover')['results'] == []
        assert len(peer.call('GET', f'/cases/{case["id"]}/jobs')) == (1 if history else 0)
        assert (first.root / 'files' / prefix / 'Ref/input.txt').read_bytes() == b'INPUT'
    result = {'passed': True, 'count': 1, 'checks': ['real_API_history_link_crash_keeps_committed_job' if history else 'real_API_link_crash_then_seal_rollback'],
              'source': str(first.root), 'fault': str(fault.root), 'peer': str(peer.root)}
    (peer.root / 'write-recovery-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    if '--child' in sys.argv:
        child(sys.argv[2], sys.argv[3])
    elif '--api' in sys.argv:
        api_check(history='--history' in sys.argv)
    else:
        main()
