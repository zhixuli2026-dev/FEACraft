"""Run the real local prepared scenario before the production fix, expecting failure."""
import asyncio,hashlib,zipfile
from functools import partial
from pathlib import Path
from live_llm_job_repeat_acceptance import main
from live_llm_local_prepared_batch_acceptance import scenario,FIXTURE

if __name__=='__main__':
    archive=Path(__file__).resolve().parents[1]/'backups/mainline-N3-07-private-cache-cleanup-20261109.zip'
    assert hashlib.sha256(archive.read_bytes()).hexdigest()=='623d45b5c87162418354b63b2af4b3337f9a51529950079a1ec6a8455cd2375d'
    with zipfile.ZipFile(archive) as original:
        assert original.read('platform/server/app/services/batch_jobs.py')==(Path(__file__).parent/'app/services/batch_jobs.py').read_bytes()
    asyncio.run(main(selected_override=partial(scenario,expected_legacy_failure=True),
        fixture_override=FIXTURE,store_volume_max=16,
        environment_overrides={'VIBEFLOW_DISTRIBUTED_JOBS':'false'}))
