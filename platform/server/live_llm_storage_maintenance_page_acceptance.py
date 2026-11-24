"""Exercise actual maintenance, account isolation, and DeepSeek evidence readback."""
import asyncio
import json
from pathlib import Path
import uuid
from urllib.parse import urlencode


FIXTURE = '''
import json
import os
import uuid
from fastapi import Depends
from sqlalchemy import event
from app.config import get_settings
from app.db import engine, get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import attempt_seal_journal as seals, storage_maintenance as maintenance

maintenance.CASE_PAGE_SIZE = 2
get_settings().storage_maintenance_enabled = (root/'enable-maintenance').exists()
(root/'maintenance-pid').write_text(str(os.getpid()))

def note(value):
    with (root/'maintenance-trace.jsonl').open('a',encoding='utf8') as stream:
        stream.write(json.dumps(value)+'\\n')

@event.listens_for(engine.sync_engine,'before_cursor_execute')
def observe_queries(conn,cursor,sql,params,context,many):
    if 'SELECT cases.id ' in sql and 'ORDER BY cases.id' in sql and 'JOIN workspaces' in sql:
        assert 'LIMIT' in sql and 3 in params
        note({'kind':'case_page','limit':3})
    if 'SELECT job_attempts.id ' in sql and 'JOIN jobs' in sql and 'jobs.case_id' in sql:
        candidates = []
        for value in params:
            if isinstance(value,str):
                try: candidates.append(str(uuid.UUID(value)))
                except ValueError: pass
        note({'kind':'attempt_ownership','targets':candidates})

maintain = maintenance.maintain_owner
async def observed(db,user_id,**kwargs):
    result = await maintain(db,user_id,**kwargs)
    note({'kind':'maintenance_completed','owner':user_id,'after':kwargs.get('after_case_id',0),
          'through':result.get('case_scan',{}).get('through_case_id'),'next':result.get('case_scan',{}).get('next_after_case_id')})
    return result
maintenance.maintain_owner = observed

@app.post('/api/v1/cases/{case_id}/testing/maintenance-cache')
async def write_test_cache(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
    case = await get_owned_case(case_id,user,db)
    identifier = str(uuid.uuid4())
    target = seals.local(case.s3_prefix,seals.workspace.PREFIX) / ('.preparing-'+identifier)
    target.mkdir(parents=True)
    (target/'test.txt').write_text('unproven cache retained')
    return {'id':identifier,'directory':str(target)}
'''


def scenario(api, *, store=None, **_):
    from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
    from live_llm_job_repeat_acceptance import write
    from live_llm_outbox_restart_acceptance import restart

    route = '/case-provisions:maintenance'
    original_id = api.case_id
    user_id = api.call('GET','/auth/me')['id']
    workspace = api.call('GET','/workspaces')[0]['id']
    cases = f'/workspaces/{workspace}/cases'
    second = write(api,'POST',cases,{'name':'Maintenance page end'})['id']
    third = write(api,'POST',cases,{'name':'Second page incomplete upload'})['id']
    other = api.call('POST','/auth/register',{'name':'Other maintenance owner',
        'email':uuid.uuid4().hex+'@example.com','password':'isolated-maintenance-123'})
    other_headers = {'Authorization':'Bearer '+other['token']}
    other_id = api.call('GET','/auth/me',extra_headers=other_headers)['id']
    other_workspace = api.call('GET','/workspaces',extra_headers=other_headers)[0]['id']
    foreign = api.call('POST',f'/workspaces/{other_workspace}/cases',{'name':'Other owner case'},extra_headers=other_headers)['id']
    def make_pending(case_id):
        return write(api,'POST',f'/cases/{case_id}/uploads',{'path':'Ref/preserved.bin','size':16,'overwrite':False})['id']
    pending = make_pending(third)
    cache = api.call('POST',f'/cases/{second}/testing/maintenance-cache') if store else None

    def trace():
        file = api.root/'maintenance-trace.jsonl'
        return [json.loads(line) for line in file.read_text(encoding='utf8').splitlines()] if file.exists() else []

    def maintenance(after=None, through=None, **options):
        query = '?' + urlencode({'after_case_id':after,'through_case_id':through}) if after is not None else ''
        return api.call('POST',route+query,**options)

    first = maintenance()
    assert first['case_scan'] == {'through_case_id':third,'next_after_case_id':second}, first
    assert not first['uploads']
    late = write(api,'POST',cases,{'name':'After first maintenance page'})['id']
    late_upload = make_pending(late)
    tail = maintenance(second,third)
    assert tail['case_scan'] == {'through_case_id':third,'next_after_case_id':None}
    assert tail['creation'] == tail['staging'] == tail['history'] == []
    assert [(row['case_id'],row['upload_id'],row['status']) for row in tail['uploads']] == [
        (third,pending,'incomplete_upload_retained')]
    # A foreign cursor cannot expand the account boundary in either direction.
    foreign_scan = maintenance(second,late,extra_headers=other_headers)
    assert foreign_scan['case_scan']['next_after_case_id'] is None and foreign_scan['uploads'] == []
    assert foreign > third and late > foreign
    api.call('POST',route+f'?after_case_id={second}',expect=422)
    api.call('POST',route+f'?after_case_id={third}&through_case_id={second}',expect=422)
    assert api.call('GET',f'/cases/{third}/uploads/{pending}')['offset'] == 0
    assert api.call('GET',f'/cases/{late}/uploads/{late_upload}')['offset'] == 0
    observed = trace()
    assert sum(row['kind']=='case_page' for row in observed) == 3, observed
    if cache:
        probes = [row['targets'] for row in observed if row['kind']=='attempt_ownership' and row['targets']]
        assert probes and all(value==[cache['id']] for value in probes), probes
        assert (Path(cache['directory'])/'test.txt').read_bytes() == b'unproven cache retained'

    # The normal lifespan starts maintenance on the next real API process.
    prior = len(observed)
    (api.root/'enable-maintenance').touch()
    kill_owned_api(api,int((api.root/'maintenance-pid').read_text()))
    restart(api)
    def background_pages():
        return [row for row in trace()[prior:] if row['kind']=='maintenance_completed']
    wait_for(lambda:len(background_pages())>=3,'Background did not drain both owners and the continuation',90)
    scheduled = background_pages()[:3]
    assert [(row['owner'],row['after']) for row in scheduled] == [(user_id,0),(other_id,0),(user_id,second)], scheduled
    assert scheduled[0]['through'] == late == scheduled[2]['through'] and scheduled[2]['next'] is None
    assert api.call('GET',f'/cases/{original_id}')['id'] == original_id
    assert api.call('GET',f'/cases/{late}/uploads/{late_upload}')['offset'] == 0
    if cache:
        assert (Path(cache['directory'])/'test.txt').read_bytes() == b'unproven cache retained'
    result = {'public_initial_page':first['case_scan'],'public_last_page':tail['case_scan'],
              'background_pages':scheduled,'foreign_owner_isolated':True,'unknown_cache_retained':bool(cache),
              'incomplete_uploads_retained':2,'case_query_limit_with_lookahead':3}
    (api.root/'maintenance-pages.json').write_text(json.dumps(result,indent=2))
    return result


if __name__ == '__main__':
    from live_llm_job_repeat_acceptance import main

    asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=8))
