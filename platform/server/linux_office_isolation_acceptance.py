"""Verify isolated PDF numbering, PPTX, narrated video and report drafts."""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile

import aiohttp
from pptx import Presentation
from pypdf import PdfReader, PdfWriter

from app.services.ppt_media_native import speaker_notes
from linux_batch_isolation_acceptance import postprocess, put
from linux_geometry_isolation_acceptance import OBSERVE_CHILD, receipt
from linux_openfoam_isolation_acceptance import finished
from linux_solver_isolation_acceptance import fetch
from linux_terminal_compute_acceptance import account, request
from mixed_load_test_support import WEB_IMAGE, Deployment, docker


IMAGE = 'vibeflow/api:linux-office-acceptance-20261126'
DECK = 'E-report/build/verified-slides.pptx'
VIDEO = 'E-report/build/verified-narration.mp4'
SLIDES = [
    {'title': 'Thermal test preparation',
     'notes': 'The controlled test is ready. Please verify the instrument readings. 请核对仪器读数。 '*5,
     'native_shapes': [{'preset': 'rect', 'x': 0, 'y': 0.8, 'width': 1, 'height': 0.2, 'fill': '#176BA2'}]},
    {'title': 'Measured performance',
     'notes': 'The peak temperature is within the limit. Record the final measurement. 温度结果符合要求。 '*5,
     'native_shapes': [{'preset': 'rect', 'x': 0, 'y': 0.8, 'width': 1, 'height': 0.2, 'fill': '#A23421'}]},
]
VERIFY_VIDEO = '''
import io, json, re, subprocess, sys
from PIL import Image

path = sys.argv[1]
info = json.loads(subprocess.check_output(
    ['ffprobe','-v','error','-show_streams','-show_format','-of','json',path]))
tracks = {stream['codec_type']: stream for stream in info['streams']}
assert len(info['streams']) == 2
assert (tracks['video']['codec_name'], tracks['audio']['codec_name']) == ('h264','aac')
assert (tracks['video']['width'], tracks['video']['height']) == (1280,720)
duration = float(info['format']['duration'])
audio = subprocess.run(['ffmpeg','-hide_banner','-i',path,'-vn','-af','volumedetect','-f','null','-'],
                       check=True, capture_output=True, text=True, timeout=60)
peak = float(re.search(r'max_volume: (-?[0-9.]+) dB',audio.stderr)[1])
assert -45 < peak <= 0
colors = []
for time in (0.5, duration-0.5):
    frame = subprocess.check_output(['ffmpeg','-v','error','-ss',f'{time:.3f}','-i',path,'-frames:v','1',
                                     '-threads','1','-f','image2pipe','-vcodec','png','-'])
    with Image.open(io.BytesIO(frame)) as picture:
        colors.append(picture.convert('RGB').getpixel((200,640)))
assert colors[0][2] > colors[0][0] and colors[1][0] > colors[1][2], colors
print(json.dumps({'duration_seconds': duration,'audio_peak_db': peak,'first_and_last_colors': colors,
                  'size': [1280,720],'video_codec':'h264','audio_codec':'aac'}))
'''


async def artifact(client, peer, owner, publication, path):
    token, case = owner
    async with client.get(peer+f'/api/v1/cases/{case}/report/derivative-artifact',
                          headers={'Authorization': 'Bearer '+token},
                          params={'publication_id': publication, 'path': path}) as response:
        assert response.status == 200, (path, response.status, await response.text())
        return await response.read()


async def published(client, peer, owner, job, path):
    selected = job['attempt']['outputs']['publication']
    assert selected['status'] == 'committed'
    publication = selected['id']
    manifest = json.loads(await fetch(client, peer, owner, f'.vibeflow/publications/{publication}/manifest.json'))
    data = await artifact(client, peer, owner, publication, path)
    assert len(data) == manifest['files'][path]['size']
    assert hashlib.sha256(data).hexdigest() == manifest['files'][path]['sha256']
    return publication, data


async def number_pdf(client, api, peer, owner, scratch):
    token, case = owner
    prefix = f'/cases/{case}'
    source_path = 'E-report/input.pdf'
    writer = PdfWriter()
    writer.add_blank_page(480, 640)
    writer.add_blank_page(500, 700).rotate(90)
    stream = io.BytesIO()
    writer.write(stream)
    source = stream.getvalue()
    upload = aiohttp.FormData()
    upload.add_field('file', source, filename='input.pdf', content_type='application/pdf')
    await request(client, api, 'POST', prefix+'/upload', token=token, params={'path': source_path}, data=upload)
    submitted = await request(client, api, 'POST', prefix+'/pdf:number', token=token, json={'input': source_path})
    job = await finished(client, peer, token, submitted['job_id'], scratch)
    publication, data = await published(client, peer, owner, job, submitted['output'])
    pages = PdfReader(io.BytesIO(data)).pages
    assert [page.extract_text().strip() for page in pages] == ['1 / 2', '2 / 2']
    assert [page.rotation for page in pages] == [0,90]
    assert await fetch(client, peer, owner, source_path) == source
    return {**receipt(job), 'publication': publication, 'pages': len(pages),
            'sha256': hashlib.sha256(data).hexdigest(), 'source_preserved': True}


async def build_deck(client, api, peer, owner, scratch):
    token, case = owner
    started = await request(client, api, 'POST', f'/cases/{case}/ppt:build', token=token,
                            json={'output': DECK, 'title': 'Thermal fixture', 'slides': SLIDES})
    job = await finished(client, peer, token, started['job_id'], scratch)
    publication, data = await published(client, peer, owner, job, DECK)
    deck = scratch/'verified-slides.pptx'
    deck.write_bytes(data)
    result = Presentation(io.BytesIO(data))
    for slide, expected in zip(result.slides, SLIDES, strict=True):
        assert any(shape.has_text_frame and shape.text == expected['title'] for shape in slide.shapes)
    assert speaker_notes(deck) == [slide['notes'].strip() for slide in SLIDES]
    return {**receipt(job), 'publication': publication, 'slides': len(result.slides),
            'sha256': hashlib.sha256(data).hexdigest()}


async def video(client, api, peer, owner, language, scratch):
    token, case = owner
    route = f'/cases/{case}/ppt:video'
    body = {'input': DECK, 'output': VIDEO, 'language': language}
    started = await request(client, api, 'POST', route, token=token, json=body)
    job = await finished(client, peer, token, started['job_id'], scratch)
    publication, data = await published(client, peer, owner, job, VIDEO)
    (scratch/'verified-narration.mp4').write_bytes(data)
    assert await fetch(client, peer, owner, DECK) == (scratch/'verified-slides.pptx').read_bytes()
    await request(client, api, 'POST', route, token=token, json=body, expected=409)
    return {**receipt(job), 'publication': publication, 'language': language,
            'sha256': hashlib.sha256(data).hexdigest()}


async def cancel_video(client, api, peer, owner, worker, scratch, old):
    token, case = owner
    path = 'E-report/build/interrupted-narration.mp4'
    started = await request(client, api, 'POST', f'/cases/{case}/ppt:video', token=token,
                            json={'input': DECK, 'output': path, 'language': old['language']})
    route = f'/jobs/{started["job_id"]}'
    async with asyncio.timeout(55):
        while True:
            job = await request(client, peer, 'GET', route, token=token)
            if job['ended_at'] is not None:
                raise AssertionError(('Video finished before cancellation', job['status']))
            identity = job['attempt'].get('process_identity')
            if identity is not None:
                parent = str(identity['pid'])
                children = await asyncio.to_thread(docker, 'exec', worker, 'python', '-B', '-c',
                                                   OBSERVE_CHILD, parent, 'ffmpeg')
                if children:
                    break
            await asyncio.sleep(.15)
    assert (await request(client, peer, 'POST', route+':cancel', token=token))['ok']
    job = await finished(client, peer, token, started['job_id'], scratch, expected='cancelled')
    assert docker('exec', worker, 'python', '-B', '-c', OBSERVE_CHILD, parent, 'ffmpeg') == ''
    assert job['attempt']['outputs'].get('publication', {}).get('status') != 'committed'
    data = await artifact(client, peer, owner, old['publication'], VIDEO)
    assert hashlib.sha256(data).hexdigest() == old['sha256']
    await request(client, peer, 'GET', f'/cases/{case}/file', token=token, params={'path': path}, expected=404)
    return {**receipt(job), 'observed_ffmpeg': [int(pid) for pid in children.splitlines()],
            'previous_video_preserved': True}


async def drafts(client, api, peer, owner, archive, scratch):
    token, case = owner
    route = f'/cases/{case}/report:compose'
    extraction = await postprocess(client, api, peer, owner, archive, scratch)
    results = {'postprocess_publication': extraction['publication']}
    for name in ('E-report/report.md','E-report/report.generated.md'):
        original = '# Previous draft\n\nKeep this exact original.\n'
        await put(client, api, owner, name, original)
        started = await request(client, api, 'POST', route, token=token, params={'wait':'false'},
                                json={'output': name, 'overwrite': True})
        job = await finished(client, peer, token, started['job_id'], scratch)
        publication = job['attempt']['outputs']['publication']['id']
        base = f'.vibeflow/publications/{publication}'
        manifest = json.loads(await fetch(client, peer, owner, base+'/manifest.json'))
        backup, = (path for path in manifest['files'] if path.endswith('/previous.md'))
        assert await fetch(client, peer, owner, base+'/files/'+backup) == original.encode('utf-8')
        generated, = (path for path in manifest['files'] if path.endswith('/report.md'))
        published = await fetch(client, peer, owner, base+'/files/'+generated)
        (scratch/Path(name).name).write_bytes(published)
        assert b'mean.p@0.2' in published and await fetch(client, peer, owner, name) == published
        results[name] = {**receipt(job), 'publication': publication, 'backup_sha256': hashlib.sha256(original.encode()).hexdigest()}
    return results


async def verify(api, peer, worker, scratch, evidence, archive, language):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=95)) as client:
        pdf_owner = await account(client, api)
        deck_owner = await account(client, peer)
        evidence['pdf'] = await number_pdf(client, api, peer, pdf_owner, scratch)
        evidence['deck'] = await build_deck(client, api, peer, deck_owner, scratch)
        evidence['video'] = await video(client, api, peer, deck_owner, language, scratch)
        evidence['cancel'] = await cancel_video(client, api, peer, deck_owner, worker, scratch, evidence['video'])
        await request(client, peer, 'GET', f'/jobs/{evidence["video"]["job"]}', token=pdf_owner[0], expected=404)
        evidence['drafts'] = await drafts(client, api, peer, pdf_owner, archive, scratch)


def main(args):
    scratch = Path(tempfile.mkdtemp(prefix='j16-office-', dir=Path(__file__).resolve().parents[2]/'tmp'))
    print('OFFICE_ISOLATION_EVIDENCE', scratch, flush=True)
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
            language = 'chinese' if args.storage == 's3' else 'english'
            asyncio.run(verify(api, peer, deployment.worker, scratch, evidence, args.foam_archive, language))
            fields = docker('run', '--rm', '--network', 'none', '--entrypoint', 'python',
                            '--mount', f'type=bind,source={scratch},target=/evidence,readonly', args.api_image,
                            '-I', '-c', VERIFY_VIDEO, '/evidence/verified-narration.mp4', timeout=90)
            evidence['video']['decoded'] = json.loads(fields)
            evidence['passed'] = True
    except Exception as error:
        evidence['failure'] = type(error).__name__+': '+str(error)[:1200]
        raise
    finally:
        evidence['original_services'] = original == set(docker('ps', '-q', '--no-trunc').splitlines())
        evidence['passed'] = evidence['passed'] and evidence['original_services']
        (scratch/'result.json').write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
        print('OFFICE_ISOLATION_RESULT', evidence['passed'], flush=True)
    assert evidence['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', choices=('fs','s3'), required=True)
    parser.add_argument('--foam-archive', type=Path, required=True)
    parser.add_argument('--api-image', default=IMAGE)
    main(parser.parse_args())
