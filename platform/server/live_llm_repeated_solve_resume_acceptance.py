"""Two actual OpenFOAM cancellations and a third-attempt continuation."""
import asyncio
from functools import partial
from live_llm_job_repeat_acceptance import main
from live_llm_solve_resume_acceptance import scenario,TIMING_FIXTURE

if __name__=='__main__':
    asyncio.run(main(selected_override=partial(scenario,repeated_continuation=True),
        fixture_override=TIMING_FIXTURE,store_volume_max=16))
