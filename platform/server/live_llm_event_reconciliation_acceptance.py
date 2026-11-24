"""N6-02 actual DeepSeek/file/index/browser/native acceptance, with hashed review.

The authorized profile is read from the existing vault without changing its DB.
Only its public identity and actual upstream receipts are saved in evidence;
the key is sent to an isolated encrypted profile and never printed or persisted
as plaintext. All model responses/tool choices come from the real service.
"""
import argparse
import asyncio
import json
import sqlite3
import sys

import case_workflow_controls_acceptance as controls
from event_reconciliation_acceptance import APP as EVENT_APP
from live_llm_acceptance import FIXTURE, source

APP = FIXTURE + EVENT_APP


def upstream_evidence(root):
    path = root / 'upstream.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []
    calls = [row for row in rows if row.get('kind') == 'http'
             and row.get('path', '').endswith('/chat/completions')]
    successful = [row for row in calls if row.get('status') == 200]
    identities = [row for row in rows if row.get('kind') == 'stream_identity']
    public_path = root / 'live-profile-public.json'
    public = json.loads(public_path.read_text(encoding='utf8')) if public_path.exists() else {}
    result = {'real_model_calls': len(calls), 'mock_llm_calls': 0,
              'real_model_successful_http': len(successful), 'actual_upstream': rows,
              'actual_provider': public.get('provider'), 'configured_model': public.get('model')}
    failures = []
    if len(successful) < 2:
        failures.append('Actual proposal and approved-tool follow-up HTTP receipts missing')
    if len(identities) < 2 or not all(row.get('id') and row.get('model') for row in identities):
        failures.append('Actual streamed model identities missing')
    if not any((row.get('usage') or {}).get('total_tokens', 0) > 0 for row in identities):
        failures.append('Actual token usage missing')
    if failures:
        result['model_evidence_failure'] = '; '.join(failures)
    (root / 'live-upstream-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    return result


def run():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--source-profile-id', type=int, required=True)
    args, rest = parser.parse_known_args()
    if args.source_profile_id != 26:
        parser.error('This acceptance uses the explicitly authorized profile26 only')
    configuration = asyncio.run(source(args.source_profile_id))
    original_server = controls.IsolatedServer

    class LiveServer(original_server):
        def __enter__(self):
            super().__enter__()
            try:
                with sqlite3.connect('file:' + (self.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
                    email = db.execute('SELECT email FROM users WHERE id=1').fetchone()[0]
                self.token = self.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
                profile = self.call('POST', '/provider-profiles', {
                    'provider': 'deepseek', 'name': 'N6-02 real event reconciliation',
                    'base_url': configuration['base_url'], 'default_model': configuration['model'],
                    'api_key': configuration['key']})
                assert configuration['key'] not in json.dumps(profile)
                public = {'id': profile['id'], 'provider': 'deepseek', 'model': configuration['model'],
                          'host': 'api.deepseek.com', 'source_profile_id': 26}
                (self.root / 'live-profile-public.json').write_text(json.dumps(public, indent=2) + '\n', encoding='utf8')
                return self
            except BaseException:
                super().__exit__(RuntimeError, None, None)
                raise

    controls.IsolatedServer = LiveServer
    controls.APP = APP
    sys.argv = [sys.argv[0], *rest]
    try:
        return controls.run(model_observer=upstream_evidence)
    finally:
        controls.IsolatedServer = original_server
        configuration.clear()


if __name__ == '__main__':
    sys.exit(run())
