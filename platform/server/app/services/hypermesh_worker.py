"""Export a sealed HyperMesh model to a deck using an installed template."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.services import attempt_workspace as workspace
from app.services.commercial_batch import run_launcher
from app.services.hypermesh_handler import EXPORTS, SUMMARY, HypermeshHandler


def export_script(export_format):
    (family, template), output = EXPORTS[export_format]
    return (
        'if {[catch {\n'
        '    set folder [file dirname [file normalize [info script]]]\n'
        '    set templates [hm_info -appinfo SPECIFIEDPATH TEMPLATES_DIR]\n'
        f'    set template [file join $templates feoutput {family} {template}]\n'
        '    if {![file isfile $template]} {error "Export template is unavailable"}\n'
        '    *readfile [file join $folder model.hm]\n'
        '    *templatefileset $template\n'
        '    *createmark nodes 1 all\n'
        '    *createmark elems 1 all\n'
        '    set nodes [hm_marklength nodes 1]\n'
        '    set elements [hm_marklength elems 1]\n'
        '    if {$nodes <= 0 || $elements <= 0} {error "The model has no finite-element mesh"}\n'
        '    *feoutputmergeincludefiles 1\n'
        f'    *feoutputwithdata $template [file join $folder {output}] 0 0 1 1 0\n'
        f'    set summary [open [file join $folder {SUMMARY}] w]\n'
        '    puts $summary [format {{"nodes":%d,"elements":%d}} $nodes $elements]\n'
        '    close $summary\n'
        '} detail options]} {\n'
        '    puts stderr $detail\n'
        '    return -options $options $detail\n'
        '}\n'
    )


def main():
    root = Path.cwd().resolve()
    handler = HypermeshHandler()
    config = handler.read_config(root)
    executable = handler.validate_launch(root, config)
    with tempfile.TemporaryDirectory(prefix='hypermesh-', dir=root / '.vibeflow') as name:
        stage = Path(name)
        model = stage / 'model.hm'
        shutil.copyfile(workspace.checked_path(root, config['input']), model)
        if workspace.digest_file(model) != config['input_record']:
            raise ValueError('HyperMesh 暂存输入摘要不一致')
        (stage / 'export.tcl').write_text(export_script(config['format']), encoding='ascii')
        arguments = ['-tcl', 'export.tcl', '-nocommand']
        if os.name != 'nt':
            arguments.append('-nobg')
        run_launcher(executable, arguments, stage, handler.name)
        handler.stage_products(root, config, stage)


if __name__ == '__main__':
    main()
