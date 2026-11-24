"""Explicit real-provider acceptance for native deadline enforcement."""
import argparse,asyncio,json,tempfile
from pathlib import Path
from native_deadline_transport_checks import verify as transport_checks
from testsupport import SERVER

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='native-deadline-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'mock_llm_calls':0,'transport_checks':[]}
    print('NATIVE_DEADLINE_EVIDENCE '+str(root),flush=True)
    try:
        report['transport_checks']=await transport_checks()
        # Real API cases and authorized model continuation are attached below.
        from sandbox_native_deadline_checks import run
        report.update(await run(args.source_profile_id,postgres=args.postgres))
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1000]};raise
    finally:(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
if __name__=='__main__':asyncio.run(main())
