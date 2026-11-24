"""Observe all recorded processes in an already-stopped isolated browser fixture."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from app.services import process_identity_native as identity
from app.services.owned_process import probe_native


async def verify(root, *, prior_timeout=False, journals=None):
    root = root.resolve()
    assert root.parent == Path(__file__).resolve().parents[2] / 'tmp'
    assert root.name.startswith('sandbox-recovery-ui-')
    assert Path(json.loads((root / 'ready.json').read_text())['root']).resolve() == root
    births = {}
    def add(distro, birth):
        births[json.dumps([distro, birth], sort_keys=True)] = (distro, birth)
    def process(value):
        add(None, value['host'])
        if value['native']:
            add(value['native']['distribution'], value['native']['process'])
    if journals is None:
        with sqlite3.connect(root / 'test.sqlite') as connection:
            journals = {'sessions':list(connection.execute('SELECT profile_json, process_json FROM native_sessions')),
                        'requests':list(connection.execute('SELECT children_json FROM native_requests'))}
    for profile, journal in journals['sessions']:
        profile, journal = json.loads(profile), json.loads(journal)
        if journal.get('root'):process(journal['root'])
        if journal.get('gate'):add(profile['distro'] or None, journal['gate']['process'])
    for (journal,) in journals['requests']:
        for child in json.loads(journal).get('children', {}).values():
            if child['identity']:process(child['identity'])
    semaphore = asyncio.Semaphore(2)
    observations = []
    async def observe(distro, birth):
        async with semaphore:
            result = await probe_native(distro, birth) if distro else identity.probe(birth)
            row = {'distribution':distro, 'birth':birth, 'observation':result,
                   'original_identity_absent':result['state'] == 'exited'}
            # Windows PID reuse is distinct from unknown scope/host mismatches.
            # Observe the current immutable creation identity; never stop the new process.
            if (not distro and birth['platform'] == 'windows'
                    and result == {'state':'mismatch','reason':'birth_or_scope_changed'}):
                try:
                    current = identity.observe(birth['pid'])
                except (ProcessLookupError, FileNotFoundError):
                    row['subsequent_observation'] = identity.probe(birth)
                    row['original_identity_absent'] = row['subsequent_observation']['state'] == 'exited'
                else:
                    row['current_identity'] = current
                    row['original_identity_absent'] = (current['platform'] == 'windows'
                        and current['host_id'] == birth['host_id'] and current['pid'] == birth['pid']
                        and int(current['creation_time_100ns']) > int(birth['creation_time_100ns']))
            observations.append(row)
            print('CLEANUP_OBSERVED ' + str(len(observations)) + '/' + str(len(births)) + ' ' + result['state'], flush=True)
    await asyncio.gather(*(observe(*value) for value in births.values()))
    result = {'all_recorded_processes_exited':all(row['observation']['state'] == 'exited' for row in observations),
              'all_original_identities_absent':all(row['original_identity_absent'] for row in observations),
              'unique_processes':len(births), 'observations':observations,
              'prior_harness_cleanup_timed_out':prior_timeout, 'llm_calls':0, 'mock_llm_calls':0}
    (root / 'cleanup.json').write_text(json.dumps(result, indent=2))
    assert result['all_original_identities_absent'], 'Original process absence not fully verified'
    print('RECOVERY_UI_CLEANUP_VERIFIED', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--prior-timeout', action='store_true')
    args = parser.parse_args()
    asyncio.run(verify(args.root, prior_timeout=args.prior_timeout))
