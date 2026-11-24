"""An actual case timeline append during registered native failure fallback."""
import asyncio
import json

from live_llm_job_repeat_acceptance import main
from live_llm_job_retry_capacity import FALLBACK_FIXTURE, fallback_race


TIMELINE_FIXTURE=r'''
from app import storage
from app.services import file_transactions
from app.services.case_events import CASE_EVENTS_PATH,append_case_event

previous_prepare=workflow_fallback._prepare
previous_capture=file_transactions._capture
timeline_cases={}

def interleaved_timeline(case,plan):
    prepared=previous_prepare(case,plan)
    marker=root/'actual-fallback-timeline-append.json'
    if route.get():
        path=storage._fs_path(case.s3_prefix,CASE_EVENTS_PATH).resolve()
        timeline_cases[path]=case
        if not marker.exists():
            event=append_case_event(case,'verification.timeline_append','isolated-retry-check')
            marker.write_text(json.dumps(event),encoding='utf8')
    return prepared

def interleaved_capture(path,destination=None,**kwargs):
    marker=root/'actual-fallback-timeline-growth.json'
    case=timeline_cases.get(path.resolve()) if route.get() and destination is not None else None
    if case is not None and not marker.exists() and kwargs.get('limit')==path.stat().st_size:
        event=append_case_event(case,'verification.timeline_growth','isolated-retry-check')
        assert path.stat().st_size>kwargs['limit']
        marker.write_text(json.dumps(event),encoding='utf8')
    return previous_capture(path,destination,**kwargs)

workflow_fallback._prepare=interleaved_timeline
file_transactions._capture=interleaved_capture
'''


def scenario(api,**kwargs):
    report=fallback_race(api,**kwargs)
    executed=json.loads((api.root/'actual-fallback-timeline-append.json').read_text(encoding='utf8'))
    growth=json.loads((api.root/'actual-fallback-timeline-growth.json').read_text(encoding='utf8'))
    previous_case=executed['case_id']
    proof=api.call('GET',f'/cases/{previous_case}/raw?path=0-caseDict/case-events.jsonl',raw=True)
    events=[json.loads(line) for line in proof.decode('utf8').splitlines()]
    assert sum(row.get('event_id')==executed['event_id'] for row in events)==1
    assert sum(row.get('event_id')==growth['event_id'] for row in events)==1
    report={**report,'intervening_events_retained_once':True,
            'timeline_event_ids':[executed['event_id'],growth['event_id']]}
    (api.root/'fallback-timeline-capacity.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario,fixture_override=FALLBACK_FIXTURE+TIMELINE_FIXTURE))
