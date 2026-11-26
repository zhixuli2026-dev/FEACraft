"""Run local STAR-CCM+ batch operations and explicitly save the final model."""
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace
from app.services.commercial_batch import run_launcher
from app.services.starccm_handler import StarccmHandler


SAVE_MACRO = 'SaveVibeFlowResult.java'
MACRO = '''package macro;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import star.common.StarMacro;

public final class SaveVibeFlowResult extends StarMacro {
    @Override
    public void execute() {
        getActiveSimulation().saveState(resolvePath("result.sim"));
        try {
            Files.write(Paths.get(resolvePath("save.complete")), new byte[] {1});
        } catch (IOException error) {
            throw new IllegalStateException("Cannot record completion of the saved simulation", error);
        }
    }
}
'''


def main():
    root = Path.cwd().resolve()
    handler = StarccmHandler()
    config = handler.read_config(root)
    executable = handler.validate_launch(root, config)
    with tempfile.TemporaryDirectory(prefix='starccm-', dir=root / '.vibeflow') as name:
        stage = Path(name)
        model = stage / 'model.sim'
        shutil.copyfile(workspace.checked_path(root, config['input']), model)
        if workspace.digest_file(model) != config['input_record']:
            raise ValueError('STAR-CCM+ 暂存输入摘要不一致')
        (stage / 'profile').mkdir()
        (stage / SAVE_MACRO).write_text(MACRO, encoding='ascii')
        steps = 'mesh,run' if config['operation'] == 'mesh_then_solve' else 'run'
        arguments = ['-np', str(config['cpus']), '-nbuserdir', 'profile', '-batch',
                     f'{steps},{SAVE_MACRO}', 'model.sim']
        run_launcher(executable, arguments, stage, handler.name)
        try:
            saved = workspace.checked_path(stage, 'save.complete')
        except FileNotFoundError as error:
            raise ValueError('STAR-CCM+ 没有执行到最终保存') from error
        with saved.open('rb') as stream:
            if stream.read(2) != b'\x01':
                raise ValueError('STAR-CCM+ 最终保存标记无效')
        handler.stage_products(root, config, stage)


if __name__ == '__main__':
    main()
