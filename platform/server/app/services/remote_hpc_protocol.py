"""Typed Slurm and OpenPBS commands for a private SSH job directory."""

import json
import re
import shlex


ACTIVE_SLURM = {
    'PENDING': 'queued', 'CONFIGURING': 'queued', 'REQUEUED': 'queued', 'REQUEUE_HOLD': 'queued',
    'RESIZING': 'running', 'RUNNING': 'running', 'COMPLETING': 'running', 'SUSPENDED': 'running',
}
FAILED_SLURM = {
    'FAILED', 'TIMEOUT', 'DEADLINE', 'BOOT_FAIL', 'NODE_FAIL', 'OUT_OF_MEMORY', 'PREEMPTED', 'REVOKED',
}
ACTIVE_PBS = {'Q':'queued', 'H':'queued', 'T':'queued', 'W':'queued', 'R':'running',
              'E':'running', 'S':'running', 'U':'running', 'B':'running'}


def scheduler_id(provider, value):
    value = value.strip()
    if provider == 'slurm':
        # sbatch --parsable optionally appends the cluster after a semicolon.
        value = value.split(';', 1)[0]
        pattern = r'[1-9][0-9]{0,17}'
    else:
        pattern = r'[0-9]{1,18}(?:\.[A-Za-z0-9][A-Za-z0-9_.-]{0,100})?'
    if re.fullmatch(pattern, value) is None:
        raise ValueError('Scheduler returned no single valid job identifier')
    return value


def submit_arguments(provider, *, name, directory, queue, seconds):
    output, error = directory+'/stdout.log', directory+'/stderr.log'
    script = directory+'/run.sh'
    if provider == 'slurm':
        args = ['sbatch', '--parsable', '--no-requeue', '--job-name', name,
                '--time', str((seconds + 59)//60), '--chdir', directory,
                '--output', output, '--error', error]
        if queue:
            args += ['--partition', queue]
    else:
        walltime = f'{seconds//3600:02d}:{seconds//60 % 60:02d}:{seconds % 60:02d}'
        args = ['qsub', '-r', 'n', '-N', name, '-l', 'walltime='+walltime, '-o', output, '-e', error]
        if queue:
            args += ['-q', queue]
    return args+[script]


def scripts(provider, *, name, directory, queue, seconds, entrypoint):
    command = shlex.join(submit_arguments(provider, name=name, directory=directory, queue=queue, seconds=seconds))
    # The caller atomically creates the claim directory and launches this
    # driver independently of its SSH channel. The claim persists on loss.
    dispatch = ('#!/bin/sh\nset -u\numask 077\n'
                f'cd {shlex.quote(directory)} || exit 125\n'
                'if [ -e .cancel ]; then\n'
                '    touch .submission/cancelled-before-submit\n'
                '    exit 0\n'
                'fi\n'
                f'{command} > .submission/raw.tmp 2> .submission/scheduler.log\n'
                'status=$?\nprintf "%s\\n" "$status" > .submission/status.tmp\n'
                'mv .submission/raw.tmp .submission/scheduler-id\n'
                'mv .submission/status.tmp .submission/status\n')
    run = ('#!/bin/sh\numask 077\n'
           f'cd {shlex.quote(directory+"/case")} || exit 125\n'
           f'/bin/bash {shlex.quote("./"+entrypoint)}\n'
           'status=$?\nprintf "%s\\n" "$status" > ../exit.tmp\n'
           'mv ../exit.tmp ../exit-code\nexit "$status"\n')
    return {'submit.sh':dispatch.encode('utf-8'), 'run.sh':run.encode('utf-8')}


def _fields(line):
    fields = list(re.finditer(r'(?:^|\s)([A-Za-z][A-Za-z0-9]*)=', line))
    return {field.group(1):line[field.end():fields[index+1].start() if index+1 < len(fields) else len(line)].strip()
            for index,field in enumerate(fields)}


async def observe(connection, provider, identifier, name, directory):
    identifier = scheduler_id(provider, identifier)
    if provider == 'slurm':
        answer = await connection.run(shlex.join(['scontrol', '-o', 'show', 'job', identifier]), timeout=15)
        if answer.exit_status != 0:
            return None
        data = _fields(answer.stdout)
        if (data.get('JobId'), data.get('JobName'), data.get('WorkDir')) != (identifier,name,directory):
            raise ValueError('Slurm observation differs from the submitted job')
        native = data['JobState']
        status = ACTIVE_SLURM.get(native)
        exit_code = None
        if status is None:
            match = re.fullmatch(r'([0-9]{1,5}):([0-9]{1,5})', data.get('ExitCode',''))
            if match is None:
                return None
            code, signal = map(int, match.groups())
            exit_code = 128+signal if signal else code
            if native == 'COMPLETED':
                status = 'succeeded' if exit_code == 0 else 'failed'
            elif native == 'CANCELLED':
                status = 'cancelled'
            elif native in FAILED_SLURM:
                status = 'failed'
    else:
        answer = await connection.run(shlex.join(['qstat', '-x', '-f', '-F', 'json', identifier]), timeout=15)
        if answer.exit_status != 0:
            return None
        data = json.loads(answer.stdout).get('Jobs', {}).get(identifier)
        if data is None or data.get('Job_Name') != name:
            raise ValueError('OpenPBS observation differs from the submitted job')
        native, exit_code = data['job_state'], None
        status = ACTIVE_PBS.get(native)
        if native in {'F', 'X'} and type(data.get('Exit_status')) is int:
            exit_code = data['Exit_status']
            status = 'succeeded' if exit_code == 0 else 'failed'
    if status is None:
        return None
    return {'state':status, 'scheduler_state':native, 'exit_code':exit_code, 'scheduler_id':identifier}


async def cancel(connection, provider, identifier):
    command = 'scancel' if provider == 'slurm' else 'qdel'
    response = await connection.run(shlex.join([command, scheduler_id(provider, identifier)]), timeout=15)
    return response.exit_status == 0
