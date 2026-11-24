"""Signed, opt-in publication of the VibeFlow browser client on a Linux web host."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from hashlib import file_digest, sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .offline_license import canonical, public_key, read_file, sign, unique_object, verify


SCHEMA = 'vibeflow.browser-client-release.v1'
VERSION_FILE = 'vibeflow-version.json'
META = 'vibeflow-client-release'
CHANNELS = ('stable', 'beta')
MAX_ARCHIVE = 384 * 1024 * 1024
MAX_SITE = 512 * 1024 * 1024
MAX_FILES = 12_000
CHUNK = 256 * 1024
DIGEST = re.compile(r'[0-9a-f]{64}')
GENERATION = re.compile(r'[0-9a-f]{32}')


class Release(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel: Literal['stable', 'beta']
    version: str = Field(pattern=r'^[0-9]+\.[0-9]+\.[0-9]+(?:-beta\.[0-9]+)?$', max_length=40)
    sequence: int = Field(strict=True,gt=0,le=2**31-1)
    archive_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    archive_bytes: int = Field(strict=True,gt=0,le=MAX_ARCHIVE)
    expires_at: int = Field(strict=True,gt=0,le=253402300799)

    @model_validator(mode='after')
    def release_channel(self):
        if ('-beta.' in self.version) != (self.channel == 'beta'):
            raise ValueError('Version and release channel differ')
        return self

    @property
    def stamp(self):
        return f'{self.channel}:{self.sequence}:{self.version}'


def file_hash(path):
    with path.open('rb') as stream:
        return file_digest(stream,'sha256').hexdigest()


def site_path(name):
    if (not isinstance(name,str) or len(name)>512 or '\\' in name or ':' in name or '\0' in name):
        raise ValueError('Invalid client asset path')
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix()!=name or not path.parts or any(
            part.startswith('.') or part.endswith((' ','.')) or any(ord(c)<32 for c in part) or part=='..'
            for part in path.parts):
        raise ValueError('Client asset must have a safe relative path')
    return path


class Entrypoints(HTMLParser):
    def __init__(self):
        super().__init__()
        self.frontend = []
        self.release = []

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag=='script' and values.get('src'):
            self.frontend.append(values['src'])
        if tag=='link' and values.get('rel') in {'stylesheet','modulepreload'} and values.get('href'):
            self.frontend.append(values['href'])
        if tag=='meta' and values.get('name')==META:
            self.release.append(values.get('content'))


def validate_site(folder, release):
    entry = Entrypoints()
    entry.feed(read_file(folder/'index.html', 256 * 1024).decode('utf8'))
    info = json.loads(read_file(folder/VERSION_FILE,4096), object_pairs_hook=unique_object)
    if entry.release != [release.stamp] or info != {'release':release.stamp,'version':release.version,
                                                   'channel':release.channel} or not entry.frontend:
        raise ValueError('Browser entry point or version does not match the signed release')
    for value in entry.frontend:
        uri = urlsplit(value)
        if uri.netloc or uri.scheme or uri.query or uri.fragment or not (folder/site_path(uri.path.lstrip('/'))).is_file():
            raise ValueError('Browser entry references an unavailable asset')


def publish(dist, feed, *, key, channel, version, sequence, expires_at):
    """Package one built web client and publish its channel metadata last."""
    provisional = Release(channel=channel, version=version, sequence=sequence,
        expires_at=expires_at, archive_bytes=1, archive_sha256='0'*64)
    dist, feed = dist.resolve(strict=True), feed.resolve()
    if feed.is_relative_to(dist) or dist.is_relative_to(feed):
        raise ValueError('Choose a release feed directory outside the web build')
    packages = feed/'packages'
    channels = feed/'channels'
    packages.mkdir(parents=True, exist_ok=True)
    channels.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.client-publish-', dir=feed) as scratch:
        archive = Path(scratch)/'client.zip'
        items = sorted(p for p in dist.rglob('*') if p.is_file())
        if len(items)>MAX_FILES-1 or sum(p.stat().st_size for p in items)>MAX_SITE-4096:
            raise ValueError('Built web client exceeds the publication limit')
        with ZipFile(archive,'x',compression=ZIP_DEFLATED) as bundle:
            for item in items:
                name = item.relative_to(dist).as_posix()
                if item.is_symlink() or not item.resolve().is_relative_to(dist):
                    raise ValueError('Web build includes a redirected asset')
                site_path(name)
                if name==VERSION_FILE:
                    raise ValueError('Version metadata is generated by the publisher')
                if name=='index.html':
                    html = read_file(item,256*1024).decode('utf8')
                    if '</head>' not in html or META in html:
                        raise ValueError('Browser index requires an unversioned HTML head')
                    line = f'<meta name="{META}" content="{provisional.stamp}">'
                    bundle.writestr(name,html.replace('</head>',line+'</head>',1))
                else:
                    bundle.write(item,name)
            bundle.writestr(VERSION_FILE,canonical({'release':provisional.stamp,'version':version,'channel':channel}))
        release = Release.model_validate({**provisional.model_dump(), 'archive_bytes':archive.stat().st_size,
                                          'archive_sha256':file_hash(archive)})
        expected = packages/(release.archive_sha256+'.zip')
        if expected.exists():
            if file_hash(expected) != release.archive_sha256:
                raise ValueError('A published artifact has unexpected content')
        else:
            os.replace(archive,expected)
        temporary = Path(scratch)/'manifest.json'
        temporary.write_bytes(canonical(sign(SCHEMA,release,key))+b'\n')
        os.replace(temporary,channels/(channel+'.json'))
    return release


def origin_url(origin):
    uri = urlsplit(origin)
    if (not uri.hostname or uri.username is not None or uri.password is not None or uri.query or uri.fragment
            or uri.path not in ('','/') or (uri.scheme!='https' and
            (uri.scheme!='http' or uri.hostname not in {'127.0.0.1','::1'}))):
        raise ValueError('Use an HTTPS release origin or an isolated loopback feed')
    return origin.rstrip('/')


def fetch(client, address, maximum, output=None):
    digest, size, chunks = sha256(), 0, []
    with client.stream('GET',address,headers={'Accept-Encoding':'identity'}) as response:
        if response.status_code!=200 or response.headers.get('content-encoding','identity')!='identity':
            raise ValueError('Release feed returned an unexpected response')
        for block in response.iter_raw(chunk_size=CHUNK):
            size += len(block)
            if size>maximum:
                raise ValueError('Release download exceeded its declared limit')
            digest.update(block)
            if output is None:
                chunks.append(block)
            else:
                output.write(block)
    return (size,digest.hexdigest()) if output is not None else b''.join(chunks)


def metadata(raw, public, *, channel=None, freshness=True):
    envelope = json.loads(raw,object_pairs_hook=unique_object)
    release = verify(SCHEMA,envelope,public,Release)
    if (channel is not None and release.channel!=channel) or (freshness and time.time()>=release.expires_at):
        raise ValueError('Release channel or expiry is invalid')
    return release


def unpack(path, folder):
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries)>MAX_FILES or sum(item.file_size for item in entries)>MAX_SITE:
            raise ValueError('Unpacked browser client exceeds its limit')
        seen = set()
        for entry in entries:
            relative = site_path(entry.filename)
            folded = relative.as_posix().casefold()
            mode = entry.external_attr >> 16
            if folded in seen or stat.S_IFMT(mode) not in (0,stat.S_IFREG):
                raise ValueError('Release contains a repeated or non-regular asset')
            seen.add(folded)
            destination = folder/relative
            destination.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(entry) as incoming, destination.open('xb') as output:
                shutil.copyfileobj(incoming,output,CHUNK)


@contextmanager
def lock(root):
    import fcntl
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.update.lock').open('ab') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def active(root):
    pointer = root/'active'
    if not pointer.exists() and not pointer.is_symlink():
        return None, {'release':None,'previous':None,'highest':{}}
    selected = os.readlink(pointer)
    if not re.fullmatch(r'activations/[0-9a-f]{32}',selected):
        raise ValueError('Active browser release pointer is invalid')
    state = json.loads(read_file(root/selected/'state.json',4096),object_pairs_hook=unique_object)
    if set(state)!={'release','previous','highest'} or not DIGEST.fullmatch(state['release']):
        raise ValueError('Active browser release state is invalid')
    return selected.split('/')[1],state


def activate(root, release, previous, highest):
    folder = root/'activations'/uuid4().hex
    folder.mkdir(parents=True)
    state = {'release':release, 'previous':previous, 'highest':highest}
    (folder/'state.json').write_bytes(canonical(state))
    (folder/'site').symlink_to('../../releases/'+release+'/site',target_is_directory=True)
    if previous is not None:
        (folder/'previous').symlink_to('../../activations/'+previous+'/site',target_is_directory=True)
    pointer = root/('.activation-'+folder.name)
    pointer.symlink_to('activations/'+folder.name,target_is_directory=True)
    os.replace(pointer,root/'active')


def verify_released(root, digest, key):
    if not DIGEST.fullmatch(digest):
        raise ValueError('Invalid installed release digest')
    folder = root/'releases'/digest
    release = metadata(read_file(folder/'manifest.json'),key,freshness=False)
    if release.archive_sha256!=digest or file_hash(folder/'client.zip')!=digest:
        raise ValueError('Installed browser release is damaged')
    with ZipFile(folder/'client.zip') as archive:
        for entry in archive.infolist():
            file = folder/'site'/site_path(entry.filename)
            if file.is_symlink() or file.stat().st_size != entry.file_size:
                raise ValueError('Installed client asset has changed')
            with archive.open(entry) as source:
                if file_digest(source,'sha256').hexdigest()!=file_hash(file):
                    raise ValueError('Installed client asset failed digest verification')
    validate_site(folder/'site',release)
    return release


def update(root, origin, channel, key, *, apply=False):
    if channel not in CHANNELS:
        raise ValueError('Invalid browser client channel')
    origin = origin_url(origin)
    with lock(root), httpx.Client(timeout=httpx.Timeout(25),follow_redirects=False) as client:
        previous,state = active(root)
        raw = fetch(client,origin+'/channels/'+channel+'.json',128*1024)
        release = metadata(raw,key,channel=channel)
        previous_high = state['highest'].get(channel)
        if previous_high and (release.sequence < previous_high[0] or
                (release.sequence==previous_high[0] and release.archive_sha256!=previous_high[1])):
            raise ValueError('Release attempts to replace trusted channel history')
        if release.archive_sha256==state['release']:
            return {'action':'current','release':release.stamp}
        if not apply:
            return {'action':'available','release':release.stamp}
        if shutil.disk_usage(root).free<release.archive_bytes+MAX_SITE:
            raise ValueError('Insufficient free disk space for a staged browser release')
        versions = root/'releases'
        versions.mkdir(exist_ok=True)
        destination = versions/release.archive_sha256
        if destination.exists():
            verify_released(root,release.archive_sha256,key)
        else:
            with tempfile.TemporaryDirectory(prefix='.client-stage-',dir=root) as temporary:
                selected = Path(temporary)/'release'
                selected.mkdir()
                archive = selected/'client.zip'
                with archive.open('xb') as output:
                    actual = fetch(client,origin+'/packages/'+release.archive_sha256+'.zip',release.archive_bytes,output)
                if actual!=(release.archive_bytes,release.archive_sha256):
                    raise ValueError('Staged client archive failed digest verification')
                unpack(archive,selected/'site')
                validate_site(selected/'site',release)
                (selected/'manifest.json').write_bytes(raw)
                os.replace(selected,destination)
        highest = {**state['highest'], channel:[release.sequence,release.archive_sha256]}
        activate(root,release.archive_sha256,previous,highest)
        return {'action':'activated','release':release.stamp}


def rollback(root,key):
    with lock(root):
        current,state = active(root)
        previous = state['previous']
        if not isinstance(previous,str) or not GENERATION.fullmatch(previous):
            raise ValueError('No prior browser release has been retained')
        old = json.loads(read_file(root/'activations'/previous/'state.json',4096),object_pairs_hook=unique_object)
        release = verify_released(root,old['release'],key)
        activate(root,release.archive_sha256,current,state['highest'])
        return {'action':'rolled_back','release':release.stamp}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation',required=True)
    publisher = operations.add_parser('publish')
    publisher.add_argument('--dist',type=Path,required=True)
    publisher.add_argument('--feed',type=Path,required=True)
    publisher.add_argument('--channel',choices=CHANNELS,default='stable')
    publisher.add_argument('--version',required=True)
    publisher.add_argument('--sequence',required=True,type=int)
    publisher.add_argument('--valid-days',type=int,default=30)
    publisher.add_argument('--private-key',required=True,type=Path)
    publisher.add_argument('--passphrase-file',type=Path)
    for name in ('check','update','rollback'):
        cmd = operations.add_parser(name)
        cmd.add_argument('--root',type=Path,required=True)
        cmd.add_argument('--public-key',type=Path,required=True)
        if name!='rollback':
            cmd.add_argument('--origin',required=True)
            cmd.add_argument('--channel',choices=CHANNELS,default='stable')
    args = parser.parse_args()
    if args.operation=='publish':
        from .offline_license_admin import password
        signing_key = serialization.load_pem_private_key(read_file(args.private_key,4096),password(args))
        if not isinstance(signing_key,Ed25519PrivateKey) or not 1<=args.valid_days<=365:
            parser.error('A publisher Ed25519 key and a validity period of 1 to 365 days are required')
        release = publish(args.dist,args.feed,key=signing_key,channel=args.channel,version=args.version,
            sequence=args.sequence,expires_at=int(time.time())+args.valid_days*86400)
        print(release.model_dump_json())
        return
    key = public_key(read_file(args.public_key,4096))
    root = args.root.resolve()
    if args.operation=='rollback':
        result = rollback(root,key)
    else:
        result = update(root,args.origin,args.channel,key,apply=args.operation=='update')
    print(json.dumps(result))


if __name__=='__main__':
    main()
