"""Verify a previous-run comparison through the registered solve head."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
from urllib.parse import urlencode

from postprocess_previous_reference_acceptance import METRIC, options
from testsupport import IsolatedServer, SERVER


def check(archive):
    from app.services.casedict import set_block

    assert archive.is_relative_to(SERVER.parents[1] / 'tmp')
    proof = json.loads((archive / 'icofoam-contract.json').read_text(encoding='utf-8'))
    assert proof['passed'] and proof['real_model_calls'] == proof['mock_llm_calls'] == 0
    prior_sources = archive / 'files'
    database = archive / 'test.sqlite'
    assert prior_sources.is_dir() and database.is_file()
    assert not any(database.with_name(database.name + suffix).exists() for suffix in ('-wal', '-shm'))
    for path in (prior_sources, *prior_sources.rglob('*')):
        info = path.lstat()
        assert not stat.S_ISLNK(info.st_mode)
        assert not getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    evidence = {'passed': False, 'model_calls': 0, 'runs': [], 'archive': str(archive)}
    api = IsolatedServer('d12-registered-reference')
    print('D12_REGISTERED_REFERENCE_EVIDENCE', api.root, flush=True)
    try:
        shutil.copytree(prior_sources, api.root / 'files')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as source:
            with closing(sqlite3.connect(api.root / 'test.sqlite')) as copy:
                source.backup(copy)
            identity = source.execute('select cases.id,users.email from cases join workspaces '
                'on workspaces.id=cases.workspace_id join users on users.id=workspaces.user_id '
                'where cases.name=?', ('Serial icoFoam original 2D cavity',)).fetchall()
        (case_id, email), = identity
        assert email.endswith('@example.com')
        with api:
            api.token = api.call('POST', '/auth/login', {
                'email': email, 'password': 'isolated-test-123',
            })['token']
            api.case_id, api.prefix = case_id, f'/cases/{case_id}'
            solve = api.call('GET', api.prefix + '/solve/results')['publication']
            assert solve['current'] and not solve['stale']
            evidence['solve_publication_id'] = solve['id']
            run_dir = 'C-run/case-1'

            def extract(label, postprocessing):
                current = api.call('GET', api.prefix + '/caseDict')
                configured = set_block(current['content'], 'postProcessing', {**postprocessing, 'runDir': run_dir})
                api.call('PUT', api.prefix + '/caseDict', {
                    'content': configured, 'expected_revision': current['dict_revision'],
                })
                headers = {'Idempotency-Key': 'registered-d12-' + label}
                job = api.call('POST', api.prefix + '/postprocess:run', {'run_dir': run_dir}, extra_headers=headers)
                assert job['publication_id']
                attempt = api.wait_job(job['job_id'], timeout=100)['attempt']
                assert attempt['process_stop']['verified'] and attempt['process_stop']['scope_empty']
                sources = attempt['workspace']['publications']
                assert sources[0]['publication_id'] == solve['id'] and sources[0]['require_current'] is True
                result = api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'publication_id': job['publication_id']}))
                evidence['runs'].append({'job_id': job['job_id'], 'publication_id': job['publication_id'],
                                         'verified_stop': True, 'inputs': sources})
                replay = api.call('POST', api.prefix + '/postprocess:run', {'run_dir': run_dir}, extra_headers=headers)
                assert replay['job_id'] == job['job_id']
                return result

            earlier = extract('initial', options())
            prior = earlier['publication']['id']
            later = extract('compared', options(prior))
            comparison, = later['metrics']['comparisons']
            provenance = comparison['reference']['provenance']
            assert provenance['publication_id'] == prior and provenance['metric'] == METRIC
            assert comparison['status'] == 'pass' and comparison['l2_absolute'] == 0
            selection, = [entry for entry in evidence['runs'][1]['inputs'] if entry['publication_id'] == prior]
            assert selection['require_current'] is False and selection['manifest_sha256'] == provenance['manifest_sha256']
            old = api.call('GET', api.prefix + '/postprocess/results?' + urlencode({'publication_id': prior}))
            assert old['metrics'] == earlier['metrics'] and not old['publication']['current']
            evidence['comparison'] = comparison
        evidence['api_exit'] = api.proc.poll()
        assert evidence['api_exit'] is not None
        evidence['passed'] = True
    except BaseException as error:
        evidence['failure'] = {'type': type(error).__name__, 'message': str(error)[:1200]}
        raise
    finally:
        (api.root / 'registered-reference.json').write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    check(parser.parse_args().archive.resolve(strict=True))
