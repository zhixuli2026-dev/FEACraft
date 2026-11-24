"""Send a real image once while PostgreSQL holds the account quota lock."""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_terminal_agent_acceptance import ROOT, VISION_OUTBOUND_OBSERVER, disposable_database
from s3testsupport import ObjectStore
from testsupport import IsolatedServer


ADVISORY_LOCK = '''
import asyncio
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from fastapi import HTTPException
from app.config import get_settings
from app.db import SessionLocal
from app.services import case_read_capacity, storage_admission

admission_original = case_read_capacity.admitted
lock_original = storage_admission.lock_owner
in_contended_admission = ContextVar('in_contended_admission', default=None)
agent_message_request = ContextVar('agent_message_request', default=False)
armed = root / 'arm-owner-lock'

@app.middleware('http')
async def identify_agent_message(request, call_next):
    selected = request.method == 'POST' and bool(re.fullmatch(r'/api/v1/conversations/[0-9]+/messages', request.url.path))
    marker = agent_message_request.set(selected)
    try:
        return await call_next(request)
    finally:
        agent_message_request.reset(marker)

async def observe_lock(db, owner, *, wait=False, settlement=False):
    active = in_contended_admission.get()
    if active and db is active[0] and wait and not settlement:
        active[1].set()
        started = time.monotonic()
        settings = get_settings()
        original_budget = settings.storage_measurement_timeout_seconds
        if active[2] == 'reject':
            settings.storage_measurement_timeout_seconds = 1.5
        try:
            await lock_original(db, owner, wait=True)
        except HTTPException as error:
            receipt({'kind': 'owner_lock_wait', 'seconds': time.monotonic() - started,
                     'status': error.status_code, 'detail': error.detail})
            raise
        else:
            receipt({'kind': 'owner_lock_wait', 'seconds': time.monotonic() - started})
        finally:
            settings.storage_measurement_timeout_seconds = original_budget
        return
    await lock_original(db, owner, wait=wait, settlement=settlement)

storage_admission.lock_owner = observe_lock

@asynccontextmanager
async def contended(db, case, **kwargs):
    async with admission_original(db, case, **kwargs) as owner:
        if not agent_message_request.get() or not armed.exists():
            yield owner
            return
        mode = armed.read_text(encoding='ascii')
        armed.unlink()
        acquired, wait_started = asyncio.Event(), asyncio.Event()

        async def competitor():
            async with SessionLocal() as other:
                await lock_original(other, owner.id, wait=True)
                receipt({'kind': 'competing_owner_lock', 'acquired': True})
                acquired.set()
                try:
                    await asyncio.wait_for(wait_started.wait(), 4)
                    await asyncio.sleep(2.2 if mode == 'reject' else .6)
                finally:
                    await other.commit()
                    receipt({'kind': 'competing_owner_lock', 'released': True})

        task = asyncio.create_task(competitor())
        marker = in_contended_admission.set((db, wait_started, mode))
        try:
            await asyncio.wait_for(acquired.wait(), 10)
            yield owner
        finally:
            in_contended_admission.reset(marker)
            await asyncio.gather(task, return_exceptions=True)

case_read_capacity.admitted = contended
'''


def verify(configuration, database_url, store, image, expected):
    api = IsolatedServer('agent-attachment-lock', app='lock_fixture:app', database_url=database_url)
    settings = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n'
    (api.root / 'lock_fixture.py').write_text(settings + FIXTURE + VISION_OUTBOUND_OBSERVER + ADVISORY_LOCK, encoding='utf8')
    (api.root / 'expected-nonce.txt').write_text(expected, encoding='utf8')
    print('AGENT_ATTACHMENT_LOCK_EVIDENCE ' + str(api.root), flush=True)
    payload = image.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    with api:
        case = api.create_case('Attachment quota-lock acceptance')
        profile = api.call('POST', '/provider-profiles', {'name': 'Isolated real vision',
            'provider': 'deepseek', 'base_url': configuration['base_url'],
            'default_model': configuration['model'], 'api_key': configuration['key']})
        conv = api.call('POST', '/conversations', {'title': 'Attachment contention', 'case_id': case['id'],
            'permission_mode': 'ask', 'provider_profile_id': profile['id'], 'model': configuration['model']})
        path = f'/conversations/{conv["id"]}'
        (api.root / 'arm-owner-lock').touch()
        request = {'provider_profile_id': profile['id'], 'model': configuration['model'],
            'content': 'Read the sole alphanumeric code shown in the attached terminal image and return it alone.',
            'images': [{'name': 'terminal.png', 'mime': 'image/png', 'data': base64.b64encode(payload).decode('ascii')}]}
        with httpx.Client(base_url=api.base + '/api/v1', timeout=100, trust_env=False,
                          headers={'Authorization': 'Bearer ' + api.token}) as client:
            response = client.post(path + '/messages', json=request)
            assert response.status_code == 200, (response.status_code, response.text[:200])
            history = client.get(path)
            history.raise_for_status()
            messages = [event['payload'] for event in history.json()['events'] if event['type'] == 'message']
            user_message, = [event for event in messages if event['role'] == 'user']
            attachment, = user_message['images']
            stored = client.get(f'/cases/{case["id"]}/raw', params={'path': attachment['path']})
            assert stored.status_code == 200 and stored.content == payload
            assert any(event['role'] == 'assistant' and event.get('content', '').strip() == expected
                       for event in messages), 'No real model image code readback'
    observed = [json.loads(line) for line in (api.root / 'upstream.jsonl').read_text(encoding='utf8').splitlines()]
    contenders = [event for event in observed if event['kind'] == 'competing_owner_lock']
    assert contenders == [{'kind': 'competing_owner_lock', 'acquired': True},
                          {'kind': 'competing_owner_lock', 'released': True}], contenders
    waited, = [event for event in observed if event['kind'] == 'owner_lock_wait']
    assert waited['seconds'] >= .5, waited
    http, = [event for event in observed if event['kind'] == 'http']
    upstream_image, = [event for event in observed if event['kind'] == 'vision_input']
    assert http['status'] == 200 and http['path'].endswith('/chat/completions'), http
    assert upstream_image['image_digests'] == [digest] and not upstream_image['nonce_in_text'], upstream_image
    result = {'passed': True, 'actual_wait_seconds': waited['seconds'], 'persisted_message_count': 1,
              'image_sha256': digest, 'actual_model': configuration['model'], 'simulated_model_requests': 0,
              'api_stopped': api.proc.poll() is not None}
    (api.root / 'attachment-lock.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print('AGENT_ATTACHMENT_LOCK_OK ' + json.dumps(result), flush=True)


async def main(args):
    configuration = await source(args.source_profile_id)
    async with disposable_database(True) as database_url:
        with ObjectStore(executable=ROOT / 'third_party/seaweed-4.52-runtime/weed', volume_max=6) as store:
            await asyncio.to_thread(verify, configuration, database_url, store, args.image, args.expected)
        assert store.proc is not None and store.proc.poll() is not None


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--expected', required=True)
    asyncio.run(main(parser.parse_args()))
