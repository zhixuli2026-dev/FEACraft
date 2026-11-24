"""Private native CLI state volumes bound to a deployment, account and thread."""

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID


@dataclass(frozen=True)
class StateVolume:
    deployment_id: UUID
    user_id: int
    thread_id: UUID
    generation: UUID

    def __post_init__(self):
        if (type(self.user_id) is not int or self.user_id < 1
                or not all(isinstance(value, UUID) for value in (self.deployment_id, self.thread_id, self.generation))):
            raise ValueError('Invalid CLI state owner')

    @property
    def binding(self):
        encoded = f'{self.deployment_id.hex}:{self.user_id}:{self.thread_id.hex}'
        return hashlib.sha256(encoded.encode('ascii')).hexdigest()

    @property
    def name(self):
        return 'vf-cli-state-' + self.binding[:40]

    @property
    def labels(self):
        return {'vibeflow.cli.state': self.binding, 'vibeflow.cli.generation': self.generation.hex}

    def provision(self, docker, image):
        labels = [argument for key, value in self.labels.items() for argument in ('--label', f'{key}={value}')]
        if docker('volume', 'create', '--driver', 'local', *labels, self.name) != self.name:
            raise ValueError('Docker returned a different CLI state volume')
        mount = self.mount(docker)
        docker('run', '--rm', '--pull=never', '--network', 'none', '--user', '0:0', '--read-only',
               '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'FOWNER',
               '--security-opt', 'no-new-privileges=true', '--mount', mount,
               '--entrypoint', 'python', image, '-I', '-c',
               'import os; os.chmod("/state", 0o700); os.chown("/state", 65534, 65534)')

    def mount(self, docker):
        inspected = json.loads(docker('volume', 'inspect', self.name))
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ValueError('Docker did not identify a unique CLI state volume')
        record = inspected[0]
        if (record.get('Name') != self.name or record.get('Driver') != 'local' or record.get('Scope') != 'local'
                or record.get('Options') or record.get('Labels') != self.labels):
            raise ValueError('Docker CLI state volume does not match its owner and generation')
        return f'type=volume,source={self.name},target=/state,volume-nocopy'

    def remove(self, docker):
        self.mount(docker)
        docker('volume', 'rm', self.name)
