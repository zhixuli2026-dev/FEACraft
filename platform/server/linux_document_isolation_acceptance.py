"""Exercise real report, citation, TeX and SyncTeX jobs inside Linux boundaries."""

import argparse
import asyncio
import base64
from contextlib import ExitStack
import hashlib
import io
import json
import re
from pathlib import Path
import tempfile
from zipfile import ZipFile

import aiohttp
from PIL import Image
from pypdf import PdfReader

from linux_geometry_isolation_acceptance import OBSERVE_CHILD
from linux_openfoam_isolation_acceptance import finished, namespace
from linux_solver_isolation_acceptance import fetch
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import WEB_IMAGE, Deployment, docker


IMAGE = 'vibeflow/api:linux-tex-v6-20261118'
TITLE = 'Isolated document calculation'
REPORT = '# '+TITLE+'\n\nThe test pressure is **42 Pa**.\n\n![Measured field](fig.png)\n'
TEX = r'''\documentclass{article}
\begin{document}
\noindent Isolated document calculation at 42 Pa.\par
%BODY%
\end{document}
'''
ALIAS = '/experiment/frozen/item.tex'
MANIFEST = '.vibeflow/publications/{}/manifest.json'
COMPILE = {'latex_compile_linux'}


async def put(client, endpoint, owner, path, content):
    token, case = owner
    return await request(client, endpoint, 'PUT', f'/cases/{case}/file', token=token,
                         json={'path': path, 'content': content})


async def artifact(client, peer, owner, kind, publication, path):
    token, case = owner
    route = f'/cases/{case}/{kind}/artifact'
    async with client.get(peer+'/api/v1'+route, headers={'Authorization': 'Bearer '+token},
                          params={'publication_id': publication, 'path': path}) as response:
        assert response.status == 200, (route, path, response.status, await response.text())
        return await response.read()


def child_receipts(job, purposes):
    attempt = job['attempt']
    children = [child for child in attempt['child_processes']['children'].values() if child['purpose'] in purposes]
    assert {child['purpose'] for child in children} == purposes
    receipts = []
    for child in children:
        stop = child['stop']
        assert stop['verified'] and stop['scope_empty']
        receipts.append({'purpose': child['purpose'], 'network': namespace(child['identity']), 'stop': stop})
    return receipts


async def report(client, api, peer, owner, scratch):
    token, case = owner
    route = f'/cases/{case}'
    image = io.BytesIO()
    Image.new('RGB', (8, 8), (85, 160, 199)).save(image, format='PNG')
    png = image.getvalue()
    form = aiohttp.FormData()
    form.add_field('file', png, filename='fig.png', content_type='image/png')
    await request(client, api, 'POST', route+'/upload', token=token, params={'path': 'E-report/fig.png'}, data=form)
    await put(client, api, owner, 'E-report/report.md', REPORT)
    submitted = await request(client, api, 'POST', route+'/report:build', token=token)
    job = await finished(client, peer, token, submitted['job_id'], scratch)
    publication = submitted['publication_id']
    manifest = json.loads(await fetch(client, peer, owner, MANIFEST.format(publication)))
    hashes = {}
    for extension in ('html', 'docx'):
        name = f'E-report/build/report.{extension}'
        data = await artifact(client, peer, owner, 'report', publication, name)
        (scratch/f'report.{extension}').write_bytes(data)
        hashes[extension] = hashlib.sha256(data).hexdigest()
        assert hashes[extension] == manifest['files'][name]['sha256']
        if extension == 'html':
            assert TITLE.encode() in re.sub(rb'\s+', b' ', data) and base64.b64encode(png) in data
        else:
            with ZipFile(io.BytesIO(data)) as archive:
                assert TITLE.encode() in archive.read('word/document.xml')
                assert any(archive.read(name) == png for name in archive.namelist() if name.startswith('word/media/'))
    bibliography = '@article{sample, author={Alice Sample}, title={Verified document}, year={2020}, journal={Sample Journal}}'
    citation = await request(client, peer, 'POST', f'/cite/cases/{case}/format', token=token,
                             json={'bibtex': bibliography, 'keys': ['sample'], 'preset': 'ieee'})
    assert 'Verified document' in citation['html'] and 'Sample' in citation['html']
    return {'job': job['id'], 'publication': publication, 'hashes': hashes,
            'children': child_receipts(job, {'pandoc_parse','pandoc_compile','pandoc_version'}), 'citation_formatted': True}


async def latex(client, api, peer, owner, recipe, scratch, *, aliases=False):
    token, case = owner
    route = f'/cases/{case}'
    main = f'E-report/latex/{recipe}.tex'
    body = r'\input{'+ALIAS+'}' if aliases else 'Plain recipe without aliases.'
    payload = {'main': main, 'recipe': recipe}
    if aliases:
        remote = 'E-report/latex/item.tex'
        await put(client, api, owner, remote, 'Frozen alias equation $a=42$.\n')
        payload['input_aliases'] = {ALIAS: remote}
    await put(client, api, owner, main, TEX.replace('%BODY%', body))
    submitted = await request(client, api, 'POST', route+'/latex:build', token=token, json=payload)
    job = await finished(client, peer, token, submitted['job_id'], scratch)
    publication = submitted['publication_id']
    manifest = json.loads(await fetch(client, peer, owner, MANIFEST.format(publication)))
    pdf_path = submitted['pdf']
    pdf = await artifact(client, peer, owner, 'latex', publication, pdf_path)
    digest = hashlib.sha256(pdf).hexdigest()
    assert digest == manifest['files'][pdf_path]['sha256']
    pages = PdfReader(io.BytesIO(pdf)).pages
    assert len(pages) == 1 and TITLE in pages[0].extract_text()
    if aliases:
        assert 'Frozen alias equation' in pages[0].extract_text()
    sync = await request(client, peer, 'POST', route+'/synctex:query', token=token,
                         json={'pdf': pdf_path, 'publication_id': publication, 'page': 1, 'x': 165, 'y': 138})
    assert sync['file'] == main and sync['line'] > 0, sync
    return {'job': job['id'], 'publication': publication, 'pdf': pdf_path, 'sha256': digest,
            'main': main, 'sync': {'file': sync['file'], 'line': sync['line']},
            'children': child_receipts(job, COMPILE | {'latex_probe_linux'})}


async def cancel_latex(client, api, peer, owner, worker, old, scratch):
    token, case = owner
    # Finite typesetting work, long enough to observe the actual pdfTeX engine.
    body = r'\newcount\fixture \loop\advance\fixture by 1 \ifnum\fixture<200000000 \repeat'
    await put(client, api, owner, old['main'], TEX.replace('%BODY%', body))
    submitted = await request(client, api, 'POST', f'/cases/{case}/latex:build', token=token,
                              json={'main': old['main'], 'recipe': 'pdflatex'})
    route = f'/jobs/{submitted["job_id"]}'
    async with asyncio.timeout(60):
        while True:
            job = await request(client, peer, 'GET', route, token=token)
            assert job['ended_at'] is None, ('Compiler exited before cancel', job['status'])
            children = job['attempt']['child_processes']['children'].values()
            running = next((child for child in children if child['purpose'] in COMPILE and child['phase'] == 'running'), None)
            if running is not None:
                parent = str(running['identity']['pid'])
                child = await asyncio.to_thread(docker, 'exec', worker, 'python', '-B', '-c',
                                                OBSERVE_CHILD, parent, 'pdflatex')
                if child:
                    break
            await asyncio.sleep(.2)
    assert (await request(client, peer, 'POST', route+':cancel', token=token))['ok']
    job = await finished(client, peer, token, submitted['job_id'], scratch, expected='cancelled')
    assert docker('exec', worker, 'python', '-B', '-c', OBSERVE_CHILD, parent, 'pdflatex') == ''
    pdf = await artifact(client, peer, owner, 'latex', old['publication'], old['pdf'])
    assert hashlib.sha256(pdf).hexdigest() == old['sha256']
    return {'job': job['id'], 'observed_engine': child, 'previous_pdf_preserved': True,
            'root_stop': job['attempt']['process_stop'], 'children': child_receipts(job, COMPILE)}


def requests_receipts(deployment):
    data = docker('exec', deployment.label+'-postgres', 'psql', '-U', 'postgres', '-d', 'replicas',
                  '-Atc', 'SELECT children_json FROM native_requests')
    purposes = {'pandoc_cite', 'synctex_query_linux'}
    receipts = []
    for row in data.splitlines():
        for child in json.loads(row).get('children', {}).values():
            if child['purpose'] in purposes:
                stop = child['stop']
                assert stop['verified'] and stop['scope_empty']
                receipts.append({'purpose': child['purpose'], 'network': namespace(child['identity']), 'stop': stop})
    assert {child['purpose'] for child in receipts} == purposes
    return receipts


async def verify(api, peer, worker, scratch, evidence, recipes, do_cancel):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=130)) as client:
        report_owner, latex_owner = await account(client, api), await account(client, peer)
        evidence['report'] = await report(client, api, peer, report_owner, scratch)
        evidence['latex'] = {}
        for recipe in recipes:
            evidence['latex'][recipe] = await latex(client, api, peer, latex_owner, recipe, scratch,
                                                     aliases=recipe=='xelatex')
        await request(client, peer, 'GET', f'/jobs/{evidence["report"]["job"]}', token=latex_owner[0], expected=404)
        if do_cancel:
            evidence['latex_cancel'] = await cancel_latex(client, api, peer, latex_owner, worker,
                                                         evidence['latex']['pdflatex'], scratch)


def main(args):
    scratch = Path(tempfile.mkdtemp(prefix='j16-documents-', dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('DOCUMENT_ISOLATION_EVIDENCE', scratch, flush=True)
    original = set(docker('ps', '-q', '--no-trunc').splitlines())
    evidence = {'passed': False, 'storage': args.storage, 'actual_model_calls': 0, 'simulated_model_calls': 0}
    try:
        with ExitStack() as stack:
            deployment = Deployment(stack, scratch, args.api_image, WEB_IMAGE)
            docker('stop', deployment.worker)
            if args.storage == 's3':
                deployment.configure_s3(scratch)
            docker('run', '--rm', '--network', 'none', '--mount', deployment.data, '--entrypoint', '/usr/bin/install',
                   args.api_image, '-d', '-o', '65534', '-g', '65534', '/work/data/files')
            deployment.worker = deployment.start_worker(deployment.label+'-isolated-worker')
            assert docker('exec', deployment.worker, 'id', '-u') == '65534'
            _, api = deployment.api(deployment.a)
            if args.storage == 's3':
                cache = deployment.volume('cold-peer-cache', '/work/data/files')
                docker('run', '--rm', '--network', 'none', '--mount', cache, '--entrypoint', '/usr/bin/chown',
                       args.api_image, '65534:65534', '/work/data/files')
                deployment.runtime.extend(('--mount', cache))
            reader, peer = deployment.api(deployment.b)
            if args.storage == 's3':
                assert docker('exec', reader, 'find', '/work/data/files', '-type', 'f') == ''
            try:
                asyncio.run(verify(api, peer, deployment.worker, scratch, evidence, args.recipes, args.cancel))
            except Exception:
                rows = docker('exec', deployment.label+'-postgres', 'psql', '-U', 'postgres', '-d', 'replicas',
                              '-Atc', 'SELECT children_json FROM native_requests')
                (scratch/'native-requests.json').write_text(rows, encoding='utf-8')
                raise
            evidence['requests'] = requests_receipts(deployment)
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:1200]
        raise
    finally:
        evidence['original_services'] = original==set(docker('ps', '-q', '--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (scratch/'result.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
        print('DOCUMENT_ISOLATION_RESULT', evidence['passed'], flush=True)
    assert evidence['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', choices=('fs','s3'), required=True)
    parser.add_argument('--recipes', nargs='+', choices=('pdflatex','xelatex','lualatex'),
                        default=('pdflatex','xelatex','lualatex'))
    parser.add_argument('--cancel', action='store_true')
    parser.add_argument('--api-image', default=IMAGE)
    main(parser.parse_args())
