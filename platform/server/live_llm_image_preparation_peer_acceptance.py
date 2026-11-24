"""Actual provider after peer competition at real preparation boundaries."""
import argparse,asyncio
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_retirement_checks import verify
from sandbox_image_preparation_peer_checks import FIXTURE,STAGES,after_retirement

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true');parser.add_argument('--stage',choices=STAGES)
    args=parser.parse_args();configuration=await source(args.source_profile_id)
    async def model(api,sid,rid,proof):return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    async def scenario(api,sid,rid,proof,continuation=None):
        return await after_retirement(api,sid,rid,proof,continuation=continuation or model,
            stages=(args.stage,) if args.stage else STAGES)
    if args.postgres:
        from sandbox_image_postgres_checks import run
        await run(fixture_prefix=LIVE_FIXTURE,execution_fixture=FIXTURE,scenario=scenario,continuation=model)
    else:await verify(fixture_prefix=LIVE_FIXTURE+FIXTURE,after_retirement=scenario)
if __name__=='__main__':asyncio.run(main())
