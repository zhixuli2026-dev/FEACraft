"""Separate submission-host observations from declared native dependencies.

These records describe provenance, not a hermetic environment or proof that a
queued program eventually ran. Native validators retain that execution proof
in the publication descriptor and its hashed toolchain/receipt files.
"""
from copy import deepcopy
from pathlib import Path
import platform
import sys

HOST_SCHEMA = 'vibeflow.attempt-host.v1'
HANDLER_SCHEMA = 'vibeflow.handler-toolchain.v1'


def host(runtime):
    from .attempt_workspace import digest_file
    executable = {'path': sys.executable}
    try:
        executable.update(digest_file(Path(sys.executable)), status='observed')
    except OSError as error:
        # Provenance collection must not turn a readable input into a failed job
        # merely because the API interpreter cannot be read on this platform.
        executable.update(status='unavailable', error_type=type(error).__name__)
    return {'schema': HOST_SCHEMA, 'scope': 'submission_host',
        'host_system': platform.system(), 'host_python': platform.python_version(),
        'host_python_implementation': platform.python_implementation(),
        'host_machine': platform.machine(), 'host_executable': executable,
        'runtime': runtime, 'native_tool_versions': 'not_observed_at_seal'}


def declared(key, version, spec, descriptor):
    from .job_execution import request_hash
    # Explicit fields only: never copy an environment, credentials, or assume
    # arbitrary source hashes are tool hashes. The descriptor digest binds all
    # other configuration, including generated settings, to this declaration.
    fields = ('worker_sha256', 'native_sha256', 'common_native_sha256',
        'runner_sha256', 'executor_sha256', 'compiler_sha256', 'builder_sha256',
        'composer_sha256', 'library_sha256', 'pandoc_sha256',
        'executable', 'executable_sha256', 'distro', 'runtime')
    components = {name: deepcopy(descriptor[name]) for name in fields if name in descriptor}
    configuration = descriptor.get('configuration', {})
    for name in ('engine_sha256', 'bashrc', 'pmix_gds'):
        if name in configuration:
            components['configuration.'+name] = deepcopy(configuration[name])
    return {'schema': HANDLER_SCHEMA, 'scope': 'prepared_handler',
        'handler': key, 'version': version, 'runtime': spec.get('runtime'),
        'execution_sha256': request_hash(spec),
        'descriptor_sha256': request_hash(descriptor), 'components': components,
        'external_dependency_closure': 'not_claimed',
        'native_observations': 'validated_publication_descriptor_and_files'}
