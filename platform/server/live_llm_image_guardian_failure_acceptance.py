"""Actual DeepSeek reads retained snapshot after a real guardian death blocks cleanup."""
import argparse
import asyncio
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE, source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_image_guardian_failure_checks import FIXTURE, after_retirement
from sandbox_retirement_checks import verify


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-profile-id',type=int,required=True)
    args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    async def model(api,sid,rid,proof):
        return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    async def scenario(api,sid,rid,proof):
        return await after_retirement(api,sid,rid,proof,continuation=model)
    await verify(fixture_prefix=LIVE_FIXTURE+FIXTURE,after_retirement=scenario)


if __name__=='__main__':asyncio.run(main())
