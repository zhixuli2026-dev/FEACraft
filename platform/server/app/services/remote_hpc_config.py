"""Operator-configured remote scheduler profiles without client-visible secrets."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

from .durable_events import json_text


_NAME = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'


@dataclass(frozen=True)
class Profile:
    name: str
    org_id: int
    scheduler: str
    root: str
    queue: str
    host: str
    port: int
    username: str
    client_key: str
    known_hosts: str
    max_seconds: int

    @property
    def fingerprint(self):
        known = Path(self.known_hosts)
        if not known.is_file() or known.stat().st_size > 1024*1024:
            raise ValueError('The deployed HPC host key file is unavailable or oversized')
        value = {**vars(self), 'known_hosts_sha256':hashlib.sha256(known.read_bytes()).hexdigest()}
        return hashlib.sha256(json_text(value).encode('utf-8')).hexdigest()

    @property
    def ssh(self):
        return {key:getattr(self,key) for key in ('host', 'port', 'username', 'client_key', 'known_hosts')}

    def public(self):
        return {'name':self.name, 'scheduler':self.scheduler, 'queue':self.queue,
                'max_seconds':self.max_seconds}


def profiles():
    inline = os.getenv('VIBEFLOW_REMOTE_HPC_PROFILES')
    config_path = os.getenv('VIBEFLOW_REMOTE_HPC_CONFIG_PATH')
    if inline and config_path:
        raise ValueError('Choose one deployment HPC profile source')
    if config_path:
        with Path(config_path).open('rb') as stream:
            raw = stream.read(128*1024+1)
    else:
        raw = (inline or '{}').encode('utf-8')
    if len(raw) > 128*1024:
        raise ValueError('Deployment HPC profile file exceeds its size limit')
    settings = json.loads(raw)
    if not isinstance(settings, dict) or len(settings) > 32:
        raise ValueError('Invalid deployment HPC profiles')
    result = {}
    for name, value in settings.items():
        if (re.fullmatch(_NAME, name) is None or not isinstance(value,dict)
                or value.keys() - {'org_id','scheduler','root','queue','host','port','username',
                                  'client_key','known_hosts','max_seconds'}):
            raise ValueError('Invalid deployment HPC profile name or fields')
        profile = Profile(name=name, **{'queue':'', 'port':22, 'max_seconds':3600, **value})
        if profile.scheduler not in {'slurm','pbs'}:
            raise ValueError('The remote scheduler must be Slurm or OpenPBS')
        root = PurePosixPath(profile.root)
        if (not root.is_absolute() or root == PurePosixPath('/') or '..' in root.parts
                or root.as_posix() != profile.root or re.search(r'[\0\r\n]',profile.root)):
            raise ValueError('The HPC root must be a canonical absolute remote directory')
        if (type(profile.org_id) is not int or profile.org_id < 1 or type(profile.port) is not int
                or not 1 <= profile.port <= 65535 or type(profile.max_seconds) is not int
                or not 60 <= profile.max_seconds <= 86400):
            raise ValueError('Invalid HPC profile organization, port, or scheduler limit')
        if (re.fullmatch(_NAME,profile.username) is None or profile.username=='root'
                or (profile.queue and re.fullmatch(_NAME,profile.queue) is None)
                or any(not isinstance(item,str) or not item or '\0' in item
                       for item in (profile.host,profile.client_key,profile.known_hosts))):
            raise ValueError('Invalid HPC account, connection, or queue settings')
        result[name] = profile
    return result
