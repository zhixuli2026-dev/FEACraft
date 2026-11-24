"""Actual DeepSeek readback after early-stage partial-write recovery and publication."""
import argparse,asyncio
from early_finalizing_acceptance import main as early_main

async def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--postgres',action='store_true');parser.add_argument('--recovery-boundary',choices=['cancel','owner_exit','capture_commit']);args=parser.parse_args()
 forwarded=['--handler','report','--boundary','before' if args.recovery_boundary else 'partial','--s3','--source-profile-id',str(args.source_profile_id)]
 if args.recovery_boundary:forwarded+=['--recovery-boundary',args.recovery_boundary]
 if args.postgres:forwarded.append('--postgres')
 return await early_main(forwarded)

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
