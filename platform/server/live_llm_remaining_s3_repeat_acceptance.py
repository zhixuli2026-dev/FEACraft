"""Explicit remaining S3 geometry-preview or PDF/PPT repeats; real model only."""
import asyncio,sys
from live_llm_job_repeat_acceptance import main,geometry_scenario
from live_llm_document_repeat_acceptance import scenario as documents,FIXTURE as DOCUMENT_FIXTURE
from live_llm_typed_repeat_acceptance import scenario as typed

if __name__=='__main__':
 geometry='--geometry' in sys.argv
 commands='--typed' in sys.argv
 if commands:sys.argv.remove('--typed')
 asyncio.run(main(selected_override=typed if commands else geometry_scenario if geometry else documents,
  fixture_override='' if geometry or commands else DOCUMENT_FIXTURE,store_volume_max=16))
