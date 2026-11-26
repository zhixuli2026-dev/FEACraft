"""Drive an owned Fluent process with a journal derived from typed parameters."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace
from app.services.commercial_batch import run_launcher
from app.services.fluent_handler import FluentHandler


def journal(config, case_file):
    commands = ['/file/set-batch-options no yes yes no', '/file/set-error-on-invalid-command yes',
                '/file/start-transcript fluent.log']
    if case_file == 'model.cas':
        commands.append('/file/cff-files? no')
    if config['data_path'] is None:
        commands.append(f'/file/read-case {case_file}')
        initialization = 'hyb-initialization' if config['initialization'] == 'hybrid' else 'initialize-flow'
        commands.append('/solve/initialize/' + initialization)
    else:
        commands.append(f'/file/read-case-data {case_file}')
    if config['time_steps'] is None:
        commands.append(f"/solve/iterate {config['iterations']}")
    else:
        commands.append(f"/solve/dual-time-iterate {config['time_steps']} {config['iterations']}")
    commands.extend(['/file/cff-files? yes', '/file/write-case-data result.cas.h5',
                     '/file/stop-transcript', '/exit yes'])
    return '\n'.join(commands) + '\n'


def main():
    root = Path.cwd().resolve()
    handler = FluentHandler()
    config = handler.read_config(root)
    executable = handler.validate_launch(root, config)
    with tempfile.TemporaryDirectory(prefix='fluent-', dir=root / '.vibeflow') as name:
        stage = Path(name)
        cff = config['input'].casefold().endswith('.h5')
        case_file = 'model.cas.h5' if cff else 'model.cas'
        files = {config['input']: case_file}
        if config['data_path'] is not None:
            files[config['data_path']] = 'model.dat.h5' if cff else 'model.dat'
        expected_inputs = handler.configured_inputs(config)
        for sealed_path, local_name in files.items():
            destination = stage / local_name
            shutil.copyfile(workspace.checked_path(root, sealed_path), destination)
            if workspace.digest_file(destination) != expected_inputs[sealed_path]:
                raise ValueError('Fluent 暂存输入摘要不一致')
        (stage / 'solve.jou').write_text(journal(config, case_file), encoding='ascii')
        arguments = [config['dimension'], '-g', f"-t{config['cpus']}"]
        if os.name == 'nt':
            arguments.append('-wait')
        run_launcher(executable, [*arguments, '-i', 'solve.jou'], stage, handler.name)
        handler.stage_products(root, config, stage)


if __name__ == '__main__':
    main()
