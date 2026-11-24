"""Installed Codex through the product API; empty private account, no inference."""
import json
import argparse
from contextlib import nullcontext
from pathlib import Path
import sqlite3

from testsupport import IsolatedServer
from codex_wsl_test_support import NativeStateFixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', choices=('windows', 'wsl'), default='windows')
    args = parser.parse_args()
    with (NativeStateFixture() if args.runtime == 'wsl' else nullcontext()), IsolatedServer('codex-session-actual') as api:
        case = api.create_case('Actual CLI metadata')
        conversation = api.call('POST', '/conversations', {'case_id': case['id'], 'permission_mode': 'plan'})
        value = api.call('POST', '/codex/sessions', {'case_id': case['id'], 'conversation_id': conversation['id'], 'runtime': args.runtime})
        assert value['phase'] == 'active' and value['process']['root'] and value['process']['bridge']
        assert value['runtime'] == args.runtime
        if args.runtime == 'wsl':
            assert value['process']['root']['native']['process']['uid'] != 0
            assert value['process']['bridge']['platform'] == 'linux' and value['process']['bridge']['uid'] != 0
        path = '/codex/sessions/' + value['id']
        assert api.call('GET', path + '/account')['account'] is None
        sandbox = api.call('GET', path + '/sandbox')
        assert sandbox['runtime'] == args.runtime and sandbox['setup_authority'] == 'deployment_operator'
        assert sandbox['legacy_readiness'] in ({'not_applicable'} if args.runtime == 'wsl' else {'ready','notConfigured','updateRequired'})
        models = api.call('GET', path + '/models')
        assert models['data']
        assert api.call('POST', path + '/thread')['thread_id']
        api.call('POST', path + '/thread', expect=409)
        api.call('DELETE', '/conversations/' + str(conversation['id']), expect=409)
        connection = sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True)
        try:
            selected, initialized = connection.execute('SELECT profile_json,initialize_json FROM native_cli_sessions WHERE id=?', (value['id'],)).fetchone()
            selected, initialized = json.loads(selected), json.loads(initialized)
        finally:
            connection.close()
        private_home = Path(selected['auth_home'])
        if args.runtime == 'wsl':
            assert str(private_home).startswith('\\\\wsl.localhost\\vibeflow-linux\\tmp\\vibeflow-cli-fixture-')
        else:
            assert private_home.is_relative_to(api.root / 'cli-state/.private-cli')
        assert not private_home.is_relative_to(api.root / 'files')
        closed = api.call('DELETE', path)
        assert closed['phase'] == 'closed' and closed['cleanup_verified']
        (api.root / 'closed-state.json').write_text(json.dumps(closed, indent=2), encoding='utf8')
        assert api.call('DELETE', path)['cleanup_verified']
        api.call('DELETE', '/conversations/' + str(conversation['id']))
        api.call('GET', path, expect=404)
        result = {'passed': True, 'actual_cli': True, 'product_api': True,
                  'initialize_user_agent': initialized['userAgent'], 'models': len(models['data']),
                  'account_present': False, 'inference_started': False, 'thread_created': True,
                  'private_account_outside_case_storage': True, 'cleanup_verified': True,
                  'conversation_delete_after_verified_close': True, 'evidence': str(api.root)}
        result.update(runtime=args.runtime, sandbox=sandbox)
    (api.root / 'actual-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
