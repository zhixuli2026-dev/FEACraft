"""Render a frozen deck and its speaker notes to a narrated MP4."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
import subprocess
import tempfile
import uuid
import wave
from xml.etree import ElementTree as Xml
from zipfile import ZipFile


CONFIG = Path('.vibeflow/prepared/ppt-media.json')
MAX_DECK = 128 * 1024 * 1024
MAX_UNPACKED = 300 * 1024 * 1024
MAX_RUNTIME = 120
VOICES = {'english': 'en-us', 'chinese': 'cmn'}
NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def record(path):
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'size': path.stat().st_size, 'sha256': digest}


def relations(archive, part):
    parent, name = posixpath.split(part)
    item = posixpath.join(parent, '_rels', name + '.rels')
    if item not in archive.namelist():
        return {}
    result = {}
    for element in Xml.fromstring(archive.read(item)):
        if element.get('TargetMode') == 'External':
            continue
        target = element.attrib['Target']
        target = posixpath.normpath(posixpath.join(parent, target)) if not target.startswith('/') else target[1:]
        if target.startswith('../') or '\\' in target:
            raise ValueError('Invalid deck relationship')
        result[element.attrib['Id']] = target, element.attrib['Type'].rsplit('/', 1)[-1]
    return result


def speaker_notes(path):
    if path.stat().st_size > MAX_DECK:
        raise ValueError('Presentation exceeds the video input limit')
    presentation = 'ppt/presentation.xml'
    with ZipFile(path) as archive:
        inventory = archive.infolist()
        names = {info.filename for info in inventory}
        if (len(names) != len(inventory) or len(inventory) > 2500
                or sum(info.file_size for info in inventory) > MAX_UNPACKED
                or any('vbaproject' in name.lower() for name in names)):
            raise ValueError('Presentation package exceeds video expansion limits or contains macros')
        slides = Xml.fromstring(archive.read(presentation)).findall('p:sldIdLst/p:sldId', NS)
        if not 1 <= len(slides) <= 50:
            raise ValueError('Video export requires 1–50 slides')
        targets = relations(archive, presentation)
        notes = []
        for slide in slides:
            part, kind = targets[slide.attrib['{' + NS['r'] + '}id']]
            if kind != 'slide':
                raise ValueError('Invalid slide relationship')
            note_part = next((item for item, kind in relations(archive, part).values() if kind == 'notesSlide'), None)
            if note_part is None:
                notes.append('')
                continue
            root = Xml.fromstring(archive.read(note_part))
            text = []
            for shape in root.findall('.//p:sp', NS):
                placeholder = shape.find('p:nvSpPr/p:nvPr/p:ph', NS)
                if placeholder is None or placeholder.get('type') != 'body':
                    continue
                for paragraph in shape.findall('p:txBody/a:p', NS):
                    text.append(''.join(item.text or '' for item in paragraph.findall('.//a:t', NS)))
            note = '\n'.join(text).strip()
            if len(note) > 10000:
                raise ValueError('Speaker notes exceed the per-slide limit')
            notes.append(note)
        if not any(notes):
            raise ValueError('Add speaker notes before exporting a narrated video')
        return notes


def command(*argv, capture=False):
    return subprocess.run(argv, check=True, timeout=MAX_RUNTIME, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else None, text=True, encoding='utf-8')


def audio(path, note_file, sentence, voice):
    if sentence:
        note_file.write_text(sentence, encoding='utf-8')
        command('espeak-ng', '-b', '1', '-v', voice, '-s', '150', '-w', str(path), '-f', str(note_file))
    else:
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\0' * 22050 * 2 * 2)
    with wave.open(str(path), 'rb') as wav:
        seconds = wav.getnframes() / wav.getframerate()
    return max(2.0, seconds + 0.35)


def inspect_media(path):
    response = command('ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path), capture=True)
    packet = json.loads(response.stdout)
    tracks = {stream['codec_type']: stream for stream in packet['streams']}
    if (set(tracks) != {'audio', 'video'} or len(packet['streams']) != 2
            or (tracks['video']['codec_name'], tracks['audio']['codec_name']) != ('h264', 'aac')
            or (tracks['video']['width'], tracks['video']['height']) != (1280, 720)):
        raise ValueError('Video lacks the expected H.264 and AAC tracks')
    return {'duration_seconds': float(packet['format']['duration']), 'video_codec': 'h264', 'audio_codec': 'aac',
            'width': 1280, 'height': 720}


def render(source, output, voice, work):
    notes = speaker_notes(source)
    for executable in ('soffice', 'pdftoppm', 'ffmpeg', 'ffprobe', 'espeak-ng'):
        if not shutil.which(executable):
            raise ValueError('PPT video tool unavailable: ' + executable)
    # Use a private profile and copy to a fixed file name before conversion.
    shutil.copyfile(source, work / 'deck.pptx')
    command('soffice', '--headless', '-env:UserInstallation=' + (work / 'profile').as_uri(),
            '--convert-to', 'pdf:impress_pdf_Export', '--outdir', str(work), str(work / 'deck.pptx'))
    command('pdftoppm', '-scale-to', '1280', '-png', str(work / 'deck.pdf'), str(work / 'slide'))
    frames = sorted(work.glob('slide-*.png'))
    if len(frames) != len(notes):
        raise ValueError('Slide render count differs from the source')
    listing, duration, timings = [], 0.0, []
    for index, (frame, sentence) in enumerate(zip(frames, notes), 1):
        wav = work / f'speech-{index:02}.wav'
        length = audio(wav, work / f'speech-{index:02}.txt', sentence, voice)
        clip = work / f'clip-{index:02}.mp4'
        start = duration
        duration += length
        if duration > 720:
            raise ValueError('Narrated video exceeds the 12-minute limit')
        command('ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-loop', '1', '-framerate', '24',
                '-i', str(frame), '-i', str(wav), '-af', 'apad', '-filter_threads', '2', '-vf',
                'scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                'pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1',
                '-t', f'{length:.3f}', '-c:v', 'libx264', '-preset', 'veryfast', '-threads', '2',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '44100', '-ac', '1', str(clip))
        listing.append("file '" + clip.name + "'")
        timings.append({'page': index, 'start_seconds': round(start, 3), 'narrated': bool(sentence),
                        'duration_seconds': round(length, 3)})
    sequence = work / 'slides.txt'
    sequence.write_text('\n'.join(listing) + '\n', encoding='ascii')
    joined = work / 'narrated.mp4'
    command('ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '1',
            '-i', str(sequence), '-c', 'copy', '-movflags', '+faststart', str(joined))
    summary = inspect_media(joined)
    if abs(summary['duration_seconds'] - duration) > max(0.25, len(notes) * 0.09):
        raise ValueError('Video timing differs from its rendered slides')
    output.parent.mkdir(parents=True, exist_ok=True)
    with joined.open('rb') as incoming, output.open('xb') as target:
        shutil.copyfileobj(incoming, target)
    return {**summary, 'pages': timings, 'voice': voice}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execution-id', required=True)
    identity = str(uuid.UUID(parser.parse_args().execution_id))
    root = Path.cwd().resolve()
    manifest = json.loads((root.parent / 'manifest.json').read_text(encoding='utf-8'))
    if root.name != 'run' or manifest['attempt_id'] != root.parent.name:
        raise ValueError('PPT video requires its owned attempt workspace')
    request = (root / CONFIG).read_bytes()
    spec = json.loads(request)
    source, output = ((root / spec[key]).resolve() for key in ('input', 'output'))
    if not source.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError('PPT video path is outside the frozen workspace')
    with tempfile.TemporaryDirectory(prefix='vf-ppt-media-') as scratch:
        result = render(source, output, VOICES[spec['language']], Path(scratch))
    receipt = root / '.vibeflow' / 'ppt-media-receipts' / (identity + '.json')
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with receipt.open('x', encoding='utf-8') as stream:
        json.dump({'schema': 'vibeflow.ppt-media.v1', 'attempt_id': manifest['attempt_id'],
                   'case_id': manifest['source']['case_id'], 'job_id': manifest['source']['job_id'],
                   'execution_id': identity, 'request_sha256': hashlib.sha256(request).hexdigest(),
                   'worker_sha256': record(Path(__file__))['sha256'], 'source': record(source),
                   'output': record(output), **result}, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
