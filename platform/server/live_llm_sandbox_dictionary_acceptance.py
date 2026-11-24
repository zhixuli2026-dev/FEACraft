"""Actual sandbox caseDict recovery, revision/phase conflicts and DeepSeek readback.

No model substitution. The optional process exit follows actual journal writes.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import time
import uuid

from live_llm_acceptance import source, FIXTURE
from live_llm_sandbox_recovery_acceptance import model_read
from live_llm_sandbox_changes_acceptance import FAULT
from sandbox_retirement_checks import verify
from testsupport import SERVER

ROOT = SERVER.parents[1]
PATH = '0-caseDict/caseDict'


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    for name, digest in json.loads(args.review.read_text('utf8'))['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    configuration = await source(args.source_profile_id)
    evidence = {'passed': False, 'mock_llm_calls': 0, 'checks': [], 'storage': 's3' if args.s3 else 'fs'}
    actual_root = None
    original = None

    async def before(api):
        nonlocal actual_root, original
        actual_root = api.root
        print('DICTIONARY_ROOT '+str(api.root), flush=True)
        original = await asyncio.to_thread(api.call, 'GET', api.prefix+'/caseDict')

    command = "from pathlib import Path; import secrets; p=Path('0-caseDict/caseDict'); p.write_text(p.read_text()+'\\n// RETIREMENT_'+secrets.token_hex(16)+'\\n')"

    async def after(api, sid, rid, _proof):
        async def call(method, path, **kw):
            return await asyncio.to_thread(api.call, method, path, **kw)

        def save():
            (api.root/'dictionary-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n', 'utf8')

        def record(name):
            evidence['checks'].append(name)
            save()

        async def read():
            return await call('GET', api.prefix+'/caseDict')

        async def put(content):
            current = await read()
            return await call('PUT', api.prefix+'/caseDict', body={'content': content, 'expected_revision': current['dict_revision']})

        async def metadata():
            return {name: await call('GET', api.prefix+'/raw?path=0-caseDict/'+name, raw=True)
                    for name in ('case-events.jsonl', 'provenance.jsonl')}

        endpoint = f'/terminal/sessions/{sid}/recoveries/{rid}'
        detail = await call('GET', endpoint)
        candidates = [p.parent for p in (api.root/'files/.terminal-recovery').glob('*/manifest.json')
                      if hashlib.sha256(p.read_bytes()).hexdigest() == detail['receipt']['manifest_sha256']]
        assert len(candidates) == 1
        target = (candidates[0]/'files'/PATH).read_bytes()
        proof = re.search(rb'RETIREMENT_[a-f0-9]{32}', target).group().decode()
        assert original['content'].encode() != target
        selected = {'paths': [PATH], 'semantics': 'case_dictionary'}

        async def preview():
            return await call('POST', endpoint+'/restore-plan', body=selected)

        def confirmation(plan):
            return {**selected, 'plan_sha256': plan['plan_sha256'], 'request_id': str(uuid.uuid4()), 'confirm': True}

        # The ordinary file endpoint remains protected, including the new bulk mode.
        for semantics in ('file_contents_only', 'sandbox_file_changes'):
            rejection = await call('POST', endpoint+'/restore-plan', body={**selected, 'semantics': semantics}, expect=409)
            assert rejection['detail']['code'] == 'recovery_protected_path'
        current = await read()
        await call('POST', api.prefix+'/caseDict:advance', body={'state': 'V', 'confirmed': True, 'expected_revision': current['dict_revision']})
        rejected = await call('POST', endpoint+'/restore-plan', body=selected, expect=409)
        assert rejected['detail']['code'] == 'recovery_dictionary_invalid'
        current = await read()
        assert current['workflow_state'] == 'V'
        await call('POST', api.prefix+'/caseDict:advance', body={'state': 'A', 'confirmed': True, 'expected_revision': current['dict_revision']})
        await put(original['content'])
        record('ordinary file protection and dedicated workflow phase gate remain enforced')

        plan = await preview()
        assert plan['files'][0]['action'] == 'overwrite'
        stale = confirmation(plan)
        await put(original['content'])  # same bytes, a new real revision
        rejected = await call('POST', endpoint+'/restore', body=stale, expect=409)
        assert rejected['detail']['code'] == 'recovery_plan_stale'
        await put(original['content']+'\n// concurrent edit\n')
        plan = await preview()
        assert plan['files'][0]['action'] == 'conflict'
        rejected = await call('POST', endpoint+'/restore', body=confirmation(plan), expect=409)
        assert rejected['detail']['code'] == 'recovery_merge_conflict'
        await put(original['content'])
        record('revision-only change invalidates preview; divergent current configuration is preserved')

        plan = await preview()
        body = confirmation(plan)
        old = await read()
        before_metadata = await metadata()
        if not args.s3:
            (api.root/'exit-bulk-restore').write_text('exit after actual second caseDict journal file', 'utf8')
            try:
                await call('POST', endpoint+'/restore', body=body)
            except (OSError, ValueError):
                pass
            else:
                raise AssertionError('Actual API death was required')
            proc = api.proc
            await asyncio.to_thread(proc.wait, 10)
            assert proc.returncode == 83
            api.proc = subprocess.Popen(proc.args, cwd=api.root, env=api.env, stdout=api.log,
                                        stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            until = time.monotonic()+40
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET', '/api/health', api_prefix=False)).get('ok'):
                        break
                except OSError:
                    pass
                assert time.monotonic() < until
                await asyncio.sleep(.2)
            assert await read() == old
            assert await metadata() == before_metadata
            await call('GET', endpoint+'/restores/'+body['request_id'], expect=404)
            record('actual mid-journal API death rolls back dictionary, both histories and SQL revision')

        receipt = await call('POST', endpoint+'/restore', body=body)
        assert receipt['written_files'] == [PATH] and receipt['dictionary_revision'] == old['dict_revision']+1
        recovered = await read()
        assert recovered['content'].encode() == target and recovered['dict_revision'] == receipt['dictionary_revision']
        history = await metadata()
        for name, data in history.items():
            assert data.startswith(before_metadata[name])
            row = json.loads(data.splitlines()[-1])
            assert row['source'] == 'sandbox-recovery' and row['revision'] == recovered['dict_revision']
            if name == 'provenance.jsonl':
                assert row['casedict_sha256'] == hashlib.sha256(target).hexdigest()
        await put(original['content']+'\n// later edit\n')
        later = await read()
        repeated = await call('POST', endpoint+'/restore', body=body)
        assert repeated['replayed'] and await read() == later
        assert (await call('GET', endpoint+'/restores/'+body['request_id']))['dictionary_revision'] == receipt['dictionary_revision']
        await put(original['content'])
        receipt = await call('POST', endpoint+'/restore', body=confirmation(await preview()))
        record('configuration, provenance, event history and revision commit together; replay preserves later edits')

        count = await asyncio.to_thread(model_read, api, sid, rid, proof, configuration,
                                        already_restored=True, path=PATH, expected_content=target)
        evidence['real_model_HTTP_calls'] = count
        record('actual DeepSeek selects read_text_file and reads unpredictable restored caseDict content')
        if args.browser:
            # Leave the actual project at its initial content so UI performs a real write.
            await put(original['content'])
            user = await call('GET', '/auth/me')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            web = ROOT/'platform/web'
            with (api.root/'web-preview.log').open('w', encoding='utf8') as log:
                preview_process = subprocess.Popen([shutil.which('node.exe') or 'node', str(web/'node_modules/vite/bin/vite.js'),
                    'preview', '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=web,
                    env=dict(os.environ, VITE_API_TARGET=api.base), stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    setup = {'web': 'http://127.0.0.1:'+str(port), 'email': user['email'], 'session_id': sid, 'recovery_id': rid, 'case_id': api.case_id}
                    (api.root/'browser-setup.json').write_text(json.dumps(setup, indent=2), 'utf8')
                    print('DICTIONARY_BROWSER '+setup['web'], flush=True)
                    until = time.monotonic()+900
                    while not (api.root/'browser-finished.json').exists():
                        assert preview_process.poll() is None and api.proc.poll() is None and time.monotonic() < until
                        await asyncio.sleep(.2)
                    browser = json.loads((api.root/'browser-finished.json').read_text('utf8'))
                    assert browser['passed']
                    evidence['browser'] = browser
                finally:
                    preview_process.terminate(); await asyncio.to_thread(preview_process.wait, 10)
                    evidence['preview_exited'] = preview_process.poll() is not None
        evidence['passed'] = True
        save()
        return count

    try:
        options = dict(fixture_prefix=FIXTURE+FAULT, before_sandbox=before,
                       sandbox_command='python3 -c '+shlex.quote(command)+'; ', after_retirement=after)
        if args.s3:
            from s3testsupport import ObjectStore
            with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
                await verify(store, **options)
            evidence['store_stopped'] = store.proc.poll() is not None
        else:
            await verify(**options)
    except BaseException as error:
        evidence['passed'] = False
        evidence['failure'] = {'type': type(error).__name__, 'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1200]}
    finally:
        if actual_root:
            (actual_root/'dictionary-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n', 'utf8')
            print('DICTIONARY_RESULT '+str(actual_root/'dictionary-result.json'), flush=True)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
