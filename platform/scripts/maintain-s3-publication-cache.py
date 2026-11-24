"""Review or evict one node's verified local cache for an S3 publication."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


SERVER = Path(__file__).resolve().parent
if not (SERVER / 'app').is_dir():
    SERVER = SERVER.parent / 'server'
sys.path.insert(0, str(SERVER))


async def run(args):
    from app import storage
    from app.db import SessionLocal, engine

    if args.failed_copy:
        from app.services.failed_publication_maintenance import load_target, review
    else:
        from app.services.publication_cache_maintenance import load_target, review

    if not storage._use_s3():
        raise ValueError('Publication cache maintenance applies only to S3')
    client = storage.s3()
    try:
        async with SessionLocal() as db:
            target = await load_target(db, args.case_id, args.publication_id)
        return review(*target, retention_days=args.retention_days, approved=args.review_sha256)
    finally:
        client.close()
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_id', type=int)
    parser.add_argument('publication_id')
    parser.add_argument('--retention-days', type=int, default=30)
    parser.add_argument('--review-sha256')
    parser.add_argument('--confirm-node-offline', action='store_true')
    parser.add_argument('--failed-copy', action='store_true')
    parser.add_argument('--confirm-storage-offline', action='store_true')
    args = parser.parse_args()
    if args.failed_copy:
        if args.confirm_node_offline or bool(args.review_sha256) != args.confirm_storage_offline:
            parser.error('Failed copy eviction requires --review-sha256 and --confirm-storage-offline')
    elif args.confirm_storage_offline or bool(args.review_sha256) != args.confirm_node_offline:
        parser.error('Node cache eviction requires --review-sha256 and --confirm-node-offline')
    os.chdir(SERVER)
    try:
        print(json.dumps(asyncio.run(run(args))))
    except (OSError, ValueError) as error:
        print(f'Publication cache maintenance refused: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
