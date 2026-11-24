"""Actual S3 staging deadline and concurrency checks followed by real DeepSeek."""
import argparse,asyncio,json
from live_llm_acceptance import FIXTURE as LIVE_FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from sandbox_retirement_checks import verify
from sandbox_staging_deadline_checks import FIXTURE,after_retirement
from s3testsupport import ObjectStore
from testsupport import SERVER


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--archive-read',action='store_true')
    modes=parser.add_mutually_exclusive_group();modes.add_argument('--concurrency',action='store_true');modes.add_argument('--restart',action='store_true')
    args=parser.parse_args()
    if args.restart and args.postgres:parser.error('The existing restart matrix uses SQLite queries; omit --postgres')
    configuration=await source(args.source_profile_id)
    if args.archive_read:
        from recovery_archive_deadline_checks import verify as verify_archive_reads
        await verify_archive_reads()
    async def model(api,sid,rid,proof):return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    selected_fixture=FIXTURE;scenario=after_retirement
    if args.restart:
        from sandbox_staging_restart_checks import FIXTURE as RESTART_FIXTURE,after_retirement as restarted
        selected_fixture=RESTART_FIXTURE;scenario=restarted
    if args.concurrency:
        from sandbox_staging_concurrency_checks import FIXTURE as CONCURRENT_FIXTURE,after_retirement as concurrent
        selected_fixture=CONCURRENT_FIXTURE
        async def scenario(api,sid,rid,proof,continuation=None):
            await concurrent(api,sid,rid,proof)
            return await continuation(api,sid,rid,proof)
    if args.postgres:
        from sandbox_staging_postgres_checks import run
        await run(fixture_prefix=LIVE_FIXTURE,execution_fixture=selected_fixture,scenario=scenario,continuation=model)
    else:
        report={'passed':False,'mock_llm_calls':0}
        async def acceptance(api,sid,rid,proof):
            report['fixture_root']=str(api.root)
            return await scenario(api,sid,rid,proof,continuation=model)
        store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed')
        try:
            with store:await verify(store,fixture_prefix=LIVE_FIXTURE+selected_fixture,after_retirement=acceptance)
            report['passed']=True
        finally:
            report['object_store_stopped']=store.proc is not None and store.proc.poll() is not None
            (store.root/'staging-deadline-run.json').write_text(json.dumps(report,indent=2),encoding='utf8')
            print('STAGING_STORE_EVIDENCE '+str(store.root),flush=True)


if __name__=='__main__':asyncio.run(main())
