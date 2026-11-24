"""Actual DeepSeek after real API exits at early scope persistence boundaries."""
import argparse
import asyncio
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE, source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_retirement_checks import verify
from sandbox_image_scope_startup_checks import FIXTURE, after_retirement


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--admitted-boundary', action='store_true')
    args = parser.parse_args()
    configuration = await source(args.source_profile_id)
    async def model(api, sid, rid, proof):
        return await asyncio.to_thread(model_read, api, sid, rid, proof, configuration)
    async def scenario(api, sid, rid, proof, continuation=None):
        return await after_retirement(api, sid, rid, proof, continuation=continuation, admitted=args.admitted_boundary)
    if args.postgres:
        from sandbox_image_postgres_checks import run
        await run(fixture_prefix=LIVE_FIXTURE, continuation=model, execution_fixture=FIXTURE, scenario=scenario)
    else:
        async def acceptance(api, sid, rid, proof):
            return await scenario(api, sid, rid, proof, continuation=model)
        await verify(fixture_prefix=LIVE_FIXTURE + FIXTURE, after_retirement=acceptance)


if __name__ == '__main__':
    asyncio.run(main())
