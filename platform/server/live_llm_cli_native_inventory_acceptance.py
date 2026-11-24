"""Actual fixed native inventory faults followed by authorized model tool readback."""
import argparse,asyncio
from live_llm_acceptance import source
from live_llm_cli_startup_acceptance import verify
from cli_native_inventory_checks import FIXTURE,verify as scenario

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    await asyncio.to_thread(verify,configuration,fixture=FIXTURE,scenario=scenario)

if __name__=='__main__':asyncio.run(main())
