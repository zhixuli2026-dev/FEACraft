"""Verify real CadQuery workflow source reads and independent database writes."""
import asyncio
from functools import partial
import sys

from live_llm_job_repeat_acceptance import main
from live_llm_workflow_retry_capacity import scenario


SOURCE_FIXTURE=r'''
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time
import uuid
from app.services import workflow_resume
from app.services.geometry_handler import GeometryHandler

source_root=Path(__file__).parent
source_invocations=ContextVar('source_invocations',default=None)
workflow_start=workflow_resume._start
actual_source=GeometryHandler.source

async def measure_resume(*args,**kwargs):
    token=source_invocations.set([])
    try:
        return await workflow_start(*args,**kwargs)
    finally:
        source_invocations.reset(token)

def measure_source(self,prefix,descriptor):
    result=actual_source(self,prefix,descriptor)
    calls=source_invocations.get()
    marker=source_root/'workflow-retry-entered.json'
    selected=2 if (source_root/'workflow-source-baseline').exists() else 1
    if calls is not None and (source_root/'workflow-retry-arm').is_file():
        calls.append(self.key)
        if len(calls)==selected and not marker.exists():
            size=result['geometry_files']['A-geo/capacity-input.bin']['size']
            ident=uuid.uuid4().hex
            temporary=marker.with_name('source-entered-'+ident+'.tmp')
            temporary.write_text(json.dumps({'id':ident,'read_bytes':size,'pid':os.getpid()}),encoding='utf8')
            temporary.replace(marker)
            deadline=time.monotonic()+35
            while not (source_root/'workflow-retry-release').is_file():
                if time.monotonic()>deadline:raise TimeoutError('Actual professional source reader did not drain')
                time.sleep(.025)
    return result

workflow_resume._start=measure_resume
GeometryHandler.source=measure_source
'''


if __name__=='__main__':
    old='--baseline' in sys.argv
    if old:sys.argv.remove('--baseline')
    changed='--change-source' in sys.argv
    if changed:
        assert not old
        sys.argv.remove('--change-source')
    assert '--s3' not in sys.argv
    fixture="(root/'workflow-source-baseline').touch()\n"+SOURCE_FIXTURE if old else SOURCE_FIXTURE
    asyncio.run(main(selected_override=partial(scenario,baseline=False,fault=changed,source_read='old' if old else 'new'),
                     fixture_override=fixture))
