"""Run the configured program within a registered local job scope."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services.abaqus_handler import AbaqusHandler, file_record, read_deck
from app.services.commercial_batch import run_launcher


def main():
    root = Path.cwd().resolve()
    handler = AbaqusHandler()
    config = handler.read_config(root)
    executable = handler.validate_launch(root, config)
    deck = read_deck(root, config['input'])
    if file_record(deck) != config['input_record']:
        raise ValueError('Abaqus 封存输入已改变')
    with tempfile.TemporaryDirectory(prefix='abaqus-', dir=root / '.vibeflow') as name:
        stage = Path(name)
        (stage / 'model.inp').write_bytes(deck)
        arguments = ['analysis', 'job=result', 'input=model.inp', f"cpus={config['cpus']}",
                     'resultsformat=odb', 'interactive', 'scratch=.']
        run_launcher(executable, arguments, stage, handler.name)
        handler.stage_products(root, config, stage)


if __name__ == '__main__':
    main()
