"""Actual Windows extraction/composition with WSL-produced fields and real LLM."""
import argparse,asyncio,hashlib,json
from pathlib import Path
from live_llm_acceptance import FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from live_llm_remote_post_report_acceptance import scenario
from testsupport import IsolatedServer,SERVER


def verify(configuration):
    api=IsolatedServer('local-post-report',app='local_post_report_fixture:app')
    (api.root/'local_post_report_fixture.py').write_text("import os\nos.environ.update(VIBEFLOW_DISTRIBUTED_JOBS='false',VIBEFLOW_JOB_EXECUTION_HOSTS='{}')\n"+FIXTURE,'utf8')
    with api:
        api.create_case('Actual local thermal extraction and reports')
        asyncio.run(scenario(api))
        content=api.call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
        model_read(api,'','',content.decode().strip(),configuration,already_restored=True,
                   path='Ref/proof.txt',expected_content=content)


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text('utf8'));assert review['entry']=='platform/server/live_llm_local_post_report_acceptance.py'
    for name,digest in review['sources'].items():assert hashlib.sha256((SERVER.parents[1]/name).read_bytes()).hexdigest()==digest,name
    await asyncio.to_thread(verify,await source(args.source_profile_id))
if __name__=='__main__':asyncio.run(main())
