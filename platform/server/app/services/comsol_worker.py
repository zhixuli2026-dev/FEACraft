"""Run one supervised COMSOL batch study in its sealed attempt."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace
from app.services.commercial_batch import run_launcher
from app.services.comsol_handler import ComsolHandler


def arguments(cpus, study):
    options = ['-np', str(cpus), '-prefsdir', 'prefs', '-configuration', 'config',
               '-data', 'workspace', '-tmpdir', 'scratch', '-recoverydir', 'recovery']
    if os.name != 'nt':
        options.append('batch')
    options.extend(['-mode', 'batch', '-inputfile', 'model.mph', '-outputfile', 'result.mph',
                    '-batchlog', 'batch.log', '-batchlogout'])
    if study is not None:
        options.extend(['-study', study])
    return options


def main():
    root = Path.cwd().resolve()
    handler = ComsolHandler()
    config = handler.read_config(root)
    executable = handler.validate_launch(root, config)
    with tempfile.TemporaryDirectory(prefix='comsol-', dir=root / '.vibeflow') as name:
        stage = Path(name)
        for directory in ('prefs', 'config', 'workspace', 'scratch', 'recovery'):
            (stage / directory).mkdir()
        model = stage / 'model.mph'
        shutil.copyfile(workspace.checked_path(root, config['input']), model)
        if workspace.digest_file(model) != config['input_record']:
            raise ValueError('COMSOL 暂存输入摘要不一致')
        run_launcher(executable, arguments(config['cpus'], config['study']), stage, handler.name)
        handler.stage_products(root, config, stage)


if __name__ == '__main__':
    main()
