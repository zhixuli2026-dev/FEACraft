"""Actual S3 resume with only the old run missing; archive-backed inputs."""
import asyncio
from functools import partial
from live_llm_job_repeat_acceptance import main
from live_llm_solve_resume_acceptance import scenario,TIMING_FIXTURE

if __name__=='__main__':
    asyncio.run(main(selected_override=partial(scenario,source_cache_mode='run'),
        fixture_override=TIMING_FIXTURE,store_volume_max=16))
