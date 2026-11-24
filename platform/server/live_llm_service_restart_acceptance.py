"""Real Agent recovery across an actual idle API process restart.

Reuses an accepted, stopped isolated host's encrypted vault and profile. No
provider output is substituted. The live observer still requires two actual
successful DeepSeek completions; loading old history alone cannot pass it.
"""
import argparse
import json
import sys
from pathlib import Path

import case_workflow_controls_acceptance as controls
from live_llm_acceptance import FIXTURE
from live_llm_event_reconciliation_acceptance import upstream_evidence

APP = FIXTURE + controls.APP


def run():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--source-root', type=Path, required=True)
    args, _ = parser.parse_known_args()
    source = args.source_root.resolve()
    assert source.is_relative_to((controls.ROOT / 'tmp').resolve()) and not source.is_symlink()
    accepted = json.loads((source / 'controls-result.json').read_text(encoding='utf-8'))
    assert accepted['passed'] and accepted['API_exit_verified'] and accepted['preview_exit_verified']
    assert accepted['all_actual_native_stops_verified'] and accepted['real_model_successful_http'] >= 2
    public = json.loads((source / 'live-profile-public.json').read_text(encoding='utf-8'))
    assert public['provider'] == 'deepseek' and public['host'] == 'api.deepseek.com'
    assert public['source_profile_id'] == 26
    vault = source / '.vault_key'
    assert vault.is_file() and not vault.is_symlink()
    original = controls.IsolatedServer

    class RestartServer(original):
        def __enter__(self):
            with (self.root / '.vault_key').open('xb') as target:
                target.write(vault.read_bytes())
            (self.root / 'live-profile-public.json').write_text(json.dumps(public, indent=2), encoding='utf-8')
            return super().__enter__()

    controls.IsolatedServer = RestartServer
    controls.APP = APP
    try:
        return controls.run(model_observer=upstream_evidence)
    finally:
        controls.IsolatedServer = original


if __name__ == '__main__':
    sys.exit(run())
