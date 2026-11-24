"""Explicit real model readback after actual OS error and HTTP-cancel recovery.

The original compiler, SQL/S3 operations and model HTTP/SSE are forwarded.
Credentials are read locally, never printed; no simulated inference result.
"""
import argparse,asyncio
from attempt_professional_finalizing_acceptance import main as professional_main


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--two-apis',action='store_true')
    args=parser.parse_args()
    forwarded=['--handler','report','--terminal-boundary','before','--mixed-error','--disconnect-validation',
        '--source-profile-id',str(args.source_profile_id)]
    if args.postgres:forwarded.append('--postgres')
    if args.two_apis:forwarded.append('--two-apis')
    return await professional_main(forwarded)


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
