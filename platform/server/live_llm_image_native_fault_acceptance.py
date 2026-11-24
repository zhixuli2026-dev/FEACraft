"""Actual DeepSeek after native unlink/receipt failures in an isolated source copy."""
import argparse,asyncio
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_image_native_fault_checks import source_copy,after_retirement
from sandbox_retirement_checks import verify

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    prefix,root=source_copy()
    print('NATIVE_IMAGE_SOURCE '+str(root),flush=True)
    async def model(api,sid,rid,proof):return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    async def scenario(api,sid,rid,proof,continuation=None):
        return await after_retirement(api,sid,rid,proof,continuation=continuation or model,source_root=root)
    if args.postgres:
        from sandbox_image_postgres_checks import run
        await run(fixture_prefix=prefix+LIVE_FIXTURE,execution_fixture='',scenario=scenario,continuation=model)
    else:await verify(fixture_prefix=prefix+LIVE_FIXTURE,after_retirement=scenario)

if __name__=='__main__':asyncio.run(main())
