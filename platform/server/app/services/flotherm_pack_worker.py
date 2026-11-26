"""Export a sealed Pack inside the registered job's supervised Windows scope."""
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace, flotherm_export
from app.services.flotherm_pack_handler import CONFIG, LOGS, RESULT, digest
from app.services.case_file_delta import snapshot
from app.services.native_execution_client import require_supervision
from app.services.postprocess_artifacts import context


def log_tails(products):
    for name in LOGS:
        path = products / name
        if path.is_file() and not path.is_symlink():
            with workspace.checked_path(products, name).open('rb') as source:
                source.seek(max(0, path.stat().st_size - 64 * 1024))
                text = source.read(64 * 1024).decode('utf8', errors='replace').strip()
                if text:
                    print(f'[{name}]\n{text}', flush=True)


def main():
    root = Path.cwd().resolve()
    identity = context(root)
    config = json.loads(workspace.checked_path(root, CONFIG).read_text(encoding='utf8'))
    launcher = flotherm_export.find_launcher(config['launcher'])
    if (snapshot(root) != config['source'] or str(launcher) != config['launcher']
            or digest(launcher) != config['launcher_sha256']
            or digest(Path(flotherm_export.__file__)) != config['engine_sha256']):
        raise ValueError('Pack 输入或 FloTHERM 运行程序已发生变化')
    pack = workspace.checked_path(root, config['pack_path'])

    def publish(stage, output, *, force):
        native = json.loads(workspace.checked_path(stage, RESULT).read_text(encoding='utf8'))
        source = config['source']['files'][config['pack_path']]
        if native['source']['sha256'] != source['sha256']:
            raise ValueError('Pack 原件摘要与执行回执不一致')
        output.mkdir(parents=True, exist_ok=True)
        for name in (*flotherm_export.EXPORTS, *LOGS):
            with workspace.checked_path(stage, name).open('rb') as stream, (output/name).open('xb') as target:
                shutil.copyfileobj(stream, target, length=1024 * 1024)
        receipt = {'schema': 'vibeflow.flotherm-pack.v1', 'case_id': identity['source']['case_id'],
                   'job_id': identity['source']['job_id'], 'attempt_id': identity['attempt_id'],
                   'execution_id': config['execution_id'], 'pack_path': config['pack_path'],
                   'pack_sha256': source['sha256'], 'return_code': native['return_code'],
                   'exported': list(flotherm_export.EXPORTS)}
        with (output/RESULT).open('x', encoding='utf8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)

    flotherm_export.export_pack(pack, root/config['output_dir'], launcher_path=str(launcher), force=False,
                               keep_stage=False, publish=publish, observe_logs=log_tails)


if __name__ == '__main__':
    with require_supervision():
        main()
