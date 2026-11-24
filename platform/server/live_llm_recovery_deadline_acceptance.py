"""Real model reads actual recovery after the complete recovery-deadline matrix."""
import argparse,asyncio
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_retirement_checks import verify
from sandbox_recovery_deadline_checks import FIXTURE,before_recovery


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    async def model(api,sid,rid,proof):return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    if args.postgres:
        from sandbox_image_postgres_checks import run
        async def scenario(api,sid,rid,proof,continuation=None):return await continuation(api,sid,rid,proof)
        await run(fixture_prefix=LIVE_FIXTURE,execution_fixture=FIXTURE,before_recovery=before_recovery,
                  scenario=scenario,continuation=model)
    else:
        await verify(fixture_prefix=LIVE_FIXTURE+FIXTURE,before_recovery=before_recovery,after_retirement=model)


if __name__=='__main__':asyncio.run(main())
