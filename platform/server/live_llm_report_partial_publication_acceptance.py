"""Crash a real report publisher after one file write and read the committed edition."""
import asyncio
import hashlib
import io
import json
import time
import uuid
from zipfile import ZipFile

from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from live_llm_job_repeat_acceptance import main, put
from live_llm_outbox_restart_acceptance import restart
from live_llm_publication_restart_acceptance import FIXTURE as PID_FIXTURE, worker_peer


FILES = ('E-report/build/report.html', 'E-report/build/report.docx')
FIXTURE = PID_FIXTURE + """
import time
from app import storage
from app.services.s3_live_store import Client

def checkpoint(path):
    if path not in ('E-report/build/report.html', 'E-report/build/report.docx'):
        return
    marker = root / 'stop-after-member'
    if not marker.exists():
        return
    marker.unlink()
    (root / 'member-written').write_text(path)
    limit = time.monotonic() + 120
    while time.monotonic() < limit:
        time.sleep(.1)
    raise TimeoutError('Publisher was never stopped')

if storage._use_s3():
    freeze = Client._freeze
    def freeze_member(self, bucket, prefix, path, body, headers):
        member = freeze(self, bucket, prefix, path, body, headers)
        checkpoint(path)
        return member
    Client._freeze = freeze_member
else:
    write_file = storage._fs_put
    def write_member(prefix, path, data, *args, **kwargs):
        result = write_file(prefix, path, data, *args, **kwargs)
        checkpoint(path)
        return result
    storage._fs_put = write_member
"""


def build(api):
    key = uuid.uuid4().hex
    deadline = time.monotonic() + 45
    while True:
        value = api.call('POST', api.prefix + '/report:build', {}, expect=(200, 202, 409),
                         extra_headers={'Idempotency-Key': key})
        if 'job_id' in value:
            return value
        assert value['detail']['code'] in {'validation_busy', 'storage_quota_busy'}, value
        if time.monotonic() >= deadline:
            raise TimeoutError('Report admission stayed busy')
        time.sleep(.1)


def edition(reader, publication_id, marker, *, current=True):
    selector = '' if current else '?publication_id=' + publication_id
    route = reader.prefix + '/report'
    result = reader.call('GET', route + '/results' + selector)
    publication = result['publication']
    assert publication['id'] == publication_id and publication['status'] == 'committed'
    assert publication['current'] == current
    hashes = {}
    for name in FILES:
        explicit = '' if current else '&publication_id=' + publication_id
        data = reader.call('GET', route + '/artifact?path=' + name + explicit, raw=True)
        digest = hashlib.sha256(data).hexdigest()
        assert digest == result['manifest']['artifacts'][name]['sha256']
        content = ZipFile(io.BytesIO(data)).read('word/document.xml') if name.endswith('.docx') else data
        assert marker.encode() in content
        hashes[name] = digest
    return hashes


def scenario(api, *, store=None, **_):
    assert api.database_url, 'Use --postgres for the independent reader'
    put(api, 'E-report/report.md', '# FIRSTEDITION\n')
    first = build(api)
    api.wait_job(first['job_id'])
    before = edition(api, first['publication_id'], 'FIRSTEDITION')
    with worker_peer(api, store) as reader:
        put(api, 'E-report/report.md', '# SECONDEDITION\n')
        (api.root / 'stop-after-member').touch()
        second = build(api)
        witness = api.root / 'member-written'
        wait_for(witness.exists, 'Publisher never reached a partial write', 120)
        kill_owned_api(api, int((api.root / 'api-pid').read_text()))
        # Read through the public routes while the process that wrote the first member is dead.
        during = edition(reader, second['publication_id'], 'SECONDEDITION')
        assert edition(reader, first['publication_id'], 'FIRSTEDITION', current=False) == before
        restart(api)
        api.wait_job(second['job_id'])
        assert edition(api, second['publication_id'], 'SECONDEDITION') == during
        for name in FILES:
            data = api.call('GET', api.prefix + '/raw?path=' + name, raw=True)
            assert hashlib.sha256(data).hexdigest() == during[name]
    evidence = {'storage': 's3' if store else 'fs', 'database': 'postgresql',
                'first': first['publication_id'], 'second': second['publication_id'],
                'interrupted_after': witness.read_text(), 'historical_hashes': before,
                'current_hashes_during_outage_and_after_recovery': during}
    (api.root / 'partial-publication.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    return evidence


if __name__ == '__main__':
    asyncio.run(main(selected_override=scenario, fixture_override=FIXTURE))
