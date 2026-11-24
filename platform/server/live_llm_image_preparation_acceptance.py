"""Real DeepSeek after image cleanup startup evidence reconciliation."""
import argparse
import asyncio
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_native_preflight_checks import FIXTURE,after_retirement
from sandbox_retirement_checks import verify

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    async def model(api,sid,rid,proof):return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    async def acceptance(api,sid,rid,proof):return await after_retirement(api,sid,rid,proof,continuation=model)
    await verify(fixture_prefix=LIVE_FIXTURE+FIXTURE,after_retirement=acceptance)

if __name__=='__main__':asyncio.run(main())
