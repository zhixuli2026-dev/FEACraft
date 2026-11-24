"""Inspect compressed history or restore original JSON before a code rollback."""
import argparse
import asyncio
import json

from app.services.reservation_history import LEDGERS, count_encoded, rewrite_page


async def main(apply):
    totals = await count_encoded()
    if not apply:
        print(json.dumps({'compressed': totals}))
        return 0
    restored = {}
    unreadable = {}
    for model in LEDGERS:
        after = None
        name = model.__tablename__
        restored[name] = unreadable[name] = 0
        while True:
            after, page = await rewrite_page(model, after, restore=True)
            restored[name] += page['rewritten']
            unreadable[name] += page['unreadable']
            if after is None:
                break
    remaining = await count_encoded()
    print(json.dumps({'restored': restored, 'unreadable': unreadable, 'still_compressed': remaining}))
    return 1 if any(remaining.values()) else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Write verified original JSON. Stop scheduled compaction first.')
    raise SystemExit(asyncio.run(main(parser.parse_args().apply)))
