"""Move exact case files and recover a scheduler receipt over authenticated SSH."""

import asyncio
import hashlib
from pathlib import PurePosixPath
import shlex
import stat
from uuid import uuid4

import asyncssh

from . import attempt_workspace, remote_hpc_protocol as scheduler


BLOCK_BYTES = 256 * 1024


async def _stat(sftp, path):
    try:
        return await sftp.lstat(path)
    except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPNoSuchPath):
        return None


async def _directory(sftp, path):
    if await _stat(sftp, path) is None:
        await sftp.makedirs(path, attrs=asyncssh.SFTPAttrs(permissions=0o700), exist_ok=True)
    result = await _stat(sftp, path)
    if result is None or not stat.S_ISDIR(result.permissions or 0):
        raise ValueError('Remote job directory is missing, redirected, or not ordinary')


async def _destination(sftp, root, relative, *, create=False):
    parts = attempt_workspace.canonical_path(relative).split('/')
    target = root
    for part in parts[:-1]:
        target += '/' + part
        if create:
            await _directory(sftp, target)
        else:
            result = await _stat(sftp, target)
            if result is None:
                return None
            if not stat.S_ISDIR(result.permissions or 0):
                raise ValueError('A remote artifact parent is not an ordinary directory')
    return target + '/' + parts[-1]


async def read(sftp, path, *, maximum):
    attrs = await _stat(sftp, path)
    if attrs is None:
        return None
    if not stat.S_ISREG(attrs.permissions or 0) or attrs.size is None or not 0 <= attrs.size <= maximum:
        raise ValueError('Remote file is redirected, special, or exceeds its size limit')
    data = bytearray()
    async with sftp.open(path, 'rb') as stream:
        while len(data) < attrs.size:
            block = await stream.read(min(BLOCK_BYTES, attrs.size - len(data)))
            if not block:
                raise ValueError('Remote file ended before its captured size')
            data.extend(block)
        if await stream.read(1):
            raise ValueError('Remote file grew during transfer')
    after = await _stat(sftp, path)
    if after is None or (after.size, after.permissions, after.mtime) != (attrs.size, attrs.permissions, attrs.mtime):
        raise ValueError('Remote file changed during transfer')
    return bytes(data)


async def _install(sftp, target, data):
    present = await read(sftp, target, maximum=len(data))
    if present is None:
        temporary = target + '.upload-' + uuid4().hex
        try:
            async with sftp.open(temporary, 'xb') as stream:
                for offset in range(0, len(data), BLOCK_BYTES):
                    await stream.write(data[offset:offset+BLOCK_BYTES])
            if await read(sftp, temporary, maximum=len(data)) != data:
                raise ValueError('SFTP bytes differ from the captured case file')
            try:
                await sftp.rename(temporary, target)
            except asyncssh.SFTPError:
                if await read(sftp, target, maximum=len(data)) != data:
                    raise
        finally:
            if await _stat(sftp, temporary) is not None:
                await sftp.remove(temporary)
    elif present != data:
        raise ValueError('An existing remote input differs from the captured case bytes')


async def prepare(connection, profile, payload, directory, name, records, read_member):
    async with connection.start_sftp_client() as sftp:
        base = str(PurePosixPath(directory).parent)
        if await sftp.realpath(base) != base:
            raise ValueError('Configured remote HPC root resolves to another directory')
        await _directory(sftp, directory)
        local_case = directory + '/case'
        await _directory(sftp, local_case)
        if await _stat(sftp, directory+'/.submission') is not None:
            return
        for path, record in records.items():
            data = await read_member(path, record)
            if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
                raise ValueError('Selected case file no longer matches the captured input')
            await _install(sftp, await _destination(sftp, local_case, path, create=True), data)
        scripts = scheduler.scripts(profile.scheduler, name=name, directory=directory, queue=profile.queue,
                                    seconds=payload['timeout_seconds'], entrypoint=payload['script'])
        for filename, data in scripts.items():
            await _install(sftp, directory+'/'+filename, data)


async def dispatch(connection, directory):
    command = (f'cd {shlex.quote(directory)} || exit 125\n'
               'umask 077\n'
               'if mkdir .submission 2>/dev/null; then\n'
               '    nohup /bin/sh ./submit.sh > .submission/dispatch.log 2>&1 < /dev/null &\n'
               'fi\n')
    result = await connection.run(command, timeout=15)
    if result.exit_status != 0:
        raise ValueError('The remote submission claim could not be opened')


async def receipt(connection, provider, directory):
    async with connection.start_sftp_client() as sftp:
        path = directory + '/.submission'
        info = await _stat(sftp, path)
        if info is None:
            return None
        if not stat.S_ISDIR(info.permissions or 0) or await sftp.realpath(path) != path:
            raise ValueError('Scheduler receipt directory was redirected')
        if await read(sftp, path+'/cancelled-before-submit', maximum=0) is not None:
            return {'cancelled_before_submit': True}
        snapshot = await read(sftp, path+'/status', maximum=32)
        if snapshot is None:
            return None
        if snapshot.strip() != b'0':
            raise ValueError('The scheduler did not confirm submission; consult the saved dispatch log')
        identifier = await read(sftp, path+'/scheduler-id', maximum=256)
        if identifier is None:
            raise ValueError('A completed remote dispatch has no job identifier')
        return {'scheduler_id': scheduler.scheduler_id(provider, identifier.decode('ascii', 'strict'))}


async def request_stop(connection, directory):
    async with connection.start_sftp_client() as sftp:
        base = str(PurePosixPath(directory).parent)
        if await sftp.realpath(base) != base:
            raise ValueError('Configured remote HPC root resolves to another directory')
        await _directory(sftp, directory)
    target = shlex.quote(directory)
    command = (f'cd {target} || exit 125\n'
               'umask 077\n'
               ': > .cancel\n'
               'if mkdir .submission 2>/dev/null; then\n'
               '    touch .submission/cancelled-before-submit\n'
               'fi\n')
    result = await connection.run(command, timeout=15)
    if result.exit_status != 0:
        raise ValueError('Remote cancellation marker was not recorded')


async def output(connection, directory, path, *, maximum):
    async with connection.start_sftp_client() as sftp:
        if await sftp.realpath(directory) != directory:
            raise ValueError('The scheduler job directory was redirected')
        if path in {'stdout.log', 'stderr.log'}:
            target = directory+'/'+path
        else:
            case = directory+'/case'
            if await sftp.realpath(case) != case:
                raise ValueError('The remote case directory was redirected')
            target = await _destination(sftp, case, path)
        if target is None:
            return None
        return await read(sftp, target, maximum=maximum)
