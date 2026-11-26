"""Summarize matched ParaView frames and check the 50-session viewport target."""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import re


USERS = 50
LARGE_CASES = 5
VIEWPORT = [480, 360]
DRAG_TARGET_MS = 200
INTERACTIVE_QUALITY = 50
QUALITIES = (INTERACTIVE_QUALITY, 100)
REFRESHES = ('push1', 'push2', 'hide_axes', 'show_axes')
SHA256 = re.compile(r'[0-9a-f]{64}')


def p95(samples):
    values = sorted(samples)
    return round(values[math.ceil(len(values) * .95) - 1], 3) if values else None


def completed_frame(row):
    elapsed = row.get('publication_ms')
    return (
        row.get('error') is None and row.get('ack_ms') is not None
        and isinstance(elapsed, (float, int)) and math.isfinite(elapsed) and elapsed >= 0
        and row.get('jpeg_count', 0) > 0 and row.get('dimensions') == VIEWPORT
    )


def analyze(receipt):
    users = receipt['target_users']
    frame_hashes = defaultdict(set)
    drags = {}
    drag_cases = {}
    large_cases = set()
    for quality in QUALITIES:
        batches = [batch['records'] for batch in receipt.get('drag_profiles', []) if batch['quality'] == quality]
        rows = [row for batch in batches for row in batch if (
            completed_frame(row) and row.get('stale') is False and SHA256.fullmatch(row.get('sha256') or '')
        )]
        cases = {row['case'] for row in rows}
        drag_cases[quality] = cases
        for row in rows:
            frame_hashes[row['case']].add(row['sha256'])
            if row['large']:
                large_cases.add(row['case'])
        phases = [row for row in rows if row.get('phase')]
        drags[quality] = {
            'frames': len(rows),
            'cases': len(cases),
            'complete': len(batches) == 3 and len(cases) == users and len(rows) == users * 3 and all(
                len(batch) == users and {row['case'] for row in batch} == cases for batch in batches
            ),
            'p95_ms': p95(row['publication_ms'] for row in rows),
            'first_capture_wall_p95_ms': p95(
                row['phase']['samples_begin_wall_cpu_stale_image'][0][1] for row in phases
            ),
            'first_capture_cpu_p95_ms': p95(
                row['phase']['samples_begin_wall_cpu_stale_image'][0][2] for row in phases
            ),
            'outside_renderer_p95_ms': p95(
                row['publication_ms'] - row['phase']['publication_ready_ms'] for row in phases
            ),
        }
    refresh_batches = {batch['name']: batch['records'] for batch in receipt.get('refreshes', [])}
    refresh_cases = {
        name: {row['case'] for row in refresh_batches.get(name, []) if completed_frame(row)}
        for name in REFRESHES
    }
    memory = receipt.get('memory_samples', [])
    same_cases = drag_cases[INTERACTIVE_QUALITY] == drag_cases[100] == set(frame_hashes)
    distinct_frames = len(frame_hashes) == users and all(len(hashes) == 6 for hashes in frame_hashes.values())
    functional = (
        receipt.get('original_containers') is True and receipt.get('live_at_once') == users
        and receipt.get('valid_first_images') == users and receipt.get('peak_renderers') == users
        and receipt.get('verified_stops') == users and bool(memory) and memory[-1]['renderers'] == 0
        and bool(SHA256.fullmatch(receipt.get('reconnected_image_sha256') or ''))
        and all(cases == set(frame_hashes) for cases in refresh_cases.values())
        and all(row['complete'] for row in drags.values())
        and same_cases and distinct_frames
    )
    return {
        'variant': receipt.get('test'),
        'users': users,
        'large_cases': len(large_cases),
        'large_cell_count': receipt.get('large_cell_count'),
        'drags': drags,
        'refreshes': {name: len(cases) for name, cases in refresh_cases.items()},
        'distinct_frames_per_case': distinct_frames,
        'verified_stops': receipt.get('verified_stops', 0),
        'peak_memory_bytes': max((row['cgroup_memory_bytes'] for row in memory), default=None),
        'settled_memory_bytes': memory[-1]['cgroup_memory_bytes'] if memory else None,
        'functional': functional,
        'interactive_target_ms': DRAG_TARGET_MS,
        'interactive_target_met': bool(
            functional and users == USERS and len(large_cases) == LARGE_CASES
            and receipt.get('large_cell_count', 0) >= 1_000_000
            and drags[INTERACTIVE_QUALITY]['p95_ms'] < DRAG_TARGET_MS
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path, nargs='+')
    parser.add_argument('--require-target', action='store_true', help='Fail if a receipt misses the 50-session target')
    args = parser.parse_args()
    results = [analyze(json.loads(path.read_text(encoding='utf-8'))) for path in args.receipt]
    print(json.dumps(results, indent=2))
    return int(args.require_target and not all(result['interactive_target_met'] for result in results))


if __name__ == '__main__':
    raise SystemExit(main())
