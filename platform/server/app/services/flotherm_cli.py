"""Offline commands for inspected source inputs and the platform IR format."""

import argparse
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile

from .flotherm_convert_job import convert
from .flotherm_import import import_sources, read_source
from .flotherm_inputs import inspect_inputs
from .flotherm_ir import validate_ir, write_ir
from .flotherm_output import (MARKER, _inventory, _locked_marker, _manifest, _new_manifest,
                              _ordinary_output, _rewrite_marker, prepare_output)
from .tree_metadata_native import checked, ordinary


def ir_file(path):
    return path/'flotherm-ir.json' if path.is_dir() else path


def read_model(path):
    value = json.loads(ir_file(path).read_text(encoding='utf-8'))
    try:
        if not isinstance(value, dict):
            raise ValueError('IR 顶层必须是 JSON 对象')
        return validate_ir(value)
    except (TypeError, AttributeError, KeyError, OverflowError, RecursionError) as error:
        raise ValueError('IR 对象、材料或关联结构无效') from error


def validate(path):
    model = read_model(path)
    return {'ok': True, 'schema': model['schema'], 'source': model['source'], 'coverage': model['coverage']}


def generate(stage, model, settings=None):
    path = None
    if settings:
        path = f'config/{settings.name}'
        (stage/'config').mkdir()
        shutil.copyfile(settings, stage/path)
    (stage/'flotherm-ir.json').write_text(json.dumps(model, allow_nan=False), encoding='utf-8')
    # The worker writes a log line. CLI stdout is reserved for the JSON result.
    with redirect_stdout(StringIO()):
        convert(stage, 'flotherm-ir.json', path, 'generated')
    return stage/'generated'


def publish_staged_case(stage, output, force):
    files, directories = _inventory(stage)
    prepare_output(output, force=force)
    with _locked_marker(output/MARKER) as stream:
        existing, existing_dirs = _inventory(output)
        if existing or existing_dirs or _manifest(stream) != _new_manifest([]):
            raise ValueError('输出目录在准备后已变化')
        for directory in sorted(directories, key=lambda name: (name.count('/'), name)):
            (output/directory).mkdir()
        for name in sorted(files):
            with (stage/name).open('rb') as source, checked(output, name).open('xb') as destination:
                shutil.copyfileobj(source, destination)
        _rewrite_marker(stream, _new_manifest(list(files)))


def build(source, settings, output, force):
    bundle = source.is_dir()
    source = ir_file(source).resolve(strict=True)
    settings = settings.resolve(strict=True) if settings else None
    output = _ordinary_output(output)
    if output.parent == output or any(path.is_relative_to(output) for path in (source, settings) if path):
        raise ValueError('输出目录不能是根目录或包含输入文件')
    if (bundle or (source.parent/MARKER).is_file()) and output.is_relative_to(source.parent):
        raise ValueError('算例输出目录不能位于 IR 输出目录内')
    with tempfile.TemporaryDirectory(prefix='flotherm-cli-') as temp:
        generated = generate(Path(temp), read_model(source), settings)
        preflight = json.loads((generated/'preflight.json').read_text(encoding='utf-8'))
        publish_staged_case(generated, output, force=force)
    return {'ok': True, 'output': str(output), 'runnable_preflight': preflight['ok'], 'blockers': preflight['blockers']}


def import_model(sources, config, output, force):
    sources = [source.resolve(strict=True) for source in sources]
    config = config.resolve(strict=True) if config else None
    output = _ordinary_output(output)
    if output.parent == output or any(source.is_relative_to(output) for source in (*sources, config) if source):
        raise ValueError('IR 输出目录不能是根目录或包含原始模型')
    settings = read_source(config, limit=1024*1024) if config else None
    model = import_sources([read_source(source) for source in sources], settings)
    with tempfile.TemporaryDirectory(prefix='flotherm-ir-') as temp:
        stage = Path(temp)
        write_ir(stage, model)
        publish_staged_case(stage, output, force=force)
    return {'ok': True, 'output': str(output/'flotherm-ir.json'), 'source': model['source'],
            'coverage': model['coverage']}


def same_generated_file(actual, expected, *, prior_conduction=False):
    current = actual.read_text(encoding='utf-8')
    canonical = expected.read_text(encoding='utf-8')
    if actual.suffix.casefold() == '.json':
        try:
            left, right = json.loads(current), json.loads(canonical)
            if actual.name == 'case-manifest.json' and isinstance(left, dict) and 'monitor_results' not in left:
                # Metadata added to v1 has no effect on the older native fields.
                right.pop('monitor_results', None)
            if actual.name in {'case-manifest.json', 'conversion-coverage.json'} and isinstance(left, dict):
                if 'source_trace' not in left:
                    right.pop('source_trace', None)
            return left == right
        except ValueError:
            return False
    if actual.name in {'Allrun.pre','Allrun.solve','Allrun.serial','Allrun.parallel','Allrun.build','Allclean'}:
        from .flotherm_case import CONDUCTION_MESH, RUN_LOCK_SCRIPT

        if prior_conduction:
            canonical = canonical.replace(CONDUCTION_MESH+' --require-ready\n','').replace(
                CONDUCTION_MESH+'\n','blockMesh\ntopoSet\n')
        return current==canonical or current==canonical.replace(RUN_LOCK_SCRIPT,'',1)
    if actual.name=='flotherm_restart.py':
        # The prior standalone runtime predates the inherited output lock.
        legacy = {'f0c6cca64ec229a479b5795ab5c91bb4777f2dc362cc796fa2c0cf4da293aa79',
                  '8a83e976450c519986288b25af1dd4c97dba84fbb38d407418401e07ac3871a2'}
        return current==canonical or hashlib.sha256(actual.read_bytes()).hexdigest() in legacy
    return current == canonical


def check(output):
    root = _ordinary_output(output)
    marker = root/MARKER
    if not marker.is_file():
        raise ValueError('缺少 FloTHERM 生成标记')
    ordinary(marker)
    with marker.open('rb') as stream:
        registered = _manifest(stream)
    if registered.get('reset_pending') is not None:
        raise ValueError('输出目录有未完成的重置')
    missing = sorted(relative for relative in registered['files'] if not checked(root, relative).is_file())
    conduction_gate = 'system/flotherm_conduction_gate.py'
    prior_conduction = conduction_gate not in registered['files']
    model = read_model(checked(root, 'flotherm-ir.json'))
    differences = []
    with tempfile.TemporaryDirectory(prefix='flotherm-check-') as temp:
        generated = generate(Path(temp), model)
        preflight = json.loads((generated/'preflight.json').read_text(encoding='utf-8'))
        for relative in _inventory(generated)[0]:
            if relative == 'mesh_gate.json':
                continue  # Written independently by the actual mesh run.
            if relative == conduction_gate and prior_conduction:
                continue
            path = checked(root, relative)
            if relative not in registered['files'] or not path.is_file() or not same_generated_file(
                    path, generated/relative, prior_conduction=prior_conduction):
                differences.append(relative)
    return {'ok': not missing and not differences and preflight['ok'], 'output': str(root),
            'missing': missing, 'changed': sorted(differences), 'blockers': preflight['blockers']}


def balance(output, report=None):
    from .flotherm_heat_balance import evaluate

    root = _ordinary_output(output)
    verified = check(root)
    if not verified['ok']:
        raise ValueError('当前算例静态检查未通过，无法核验热平衡')
    manifest = json.loads(checked(root, 'case-manifest.json').read_text(encoding='utf8'))
    result = evaluate(root, read_model(checked(root, 'flotherm-ir.json')), manifest.get('monitor_results', []))
    return _write_report(result, report)


def _write_report(result, report):
    if report is not None:
        with report.open('x', encoding='utf8') as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
    return result


def run(arguments):
    parser = argparse.ArgumentParser(prog='flotherm2foam')
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect', help='预检原始输入组合或现有 IR')
    inspect.add_argument('input', type=Path)
    validation = commands.add_parser('validate', help='验证 FloTHERM IR 文件或目录')
    validation.add_argument('input', type=Path)
    exporting = commands.add_parser('export', help='用本机有许可证的 FloTHERM 导出 Pack')
    exporting.add_argument('input', type=Path)
    exporting.add_argument('--output', type=Path, required=True)
    exporting.add_argument('--flotherm-bat')
    exporting.add_argument('--keep-stage', action='store_true')
    exporting.add_argument('--force', action='store_true')
    importing = commands.add_parser('import', help='导入 ECXML、PDML 和 XTXML 配对装配')
    importing.add_argument('input', type=Path)
    importing.add_argument('--config', type=Path)
    importing.add_argument('--output', type=Path, required=True)
    importing.add_argument('--force', action='store_true')
    for command in (inspect, importing):
        for option in ('pdml', 'ecxml', 'xtxml'):
            command.add_argument('--'+option, type=Path)
    building = commands.add_parser('build', help='使用现有 IR 生成 OpenFOAM 算例')
    building.add_argument('input', type=Path)
    building.add_argument('--config', type=Path)
    building.add_argument('--output', type=Path, required=True)
    building.add_argument('--force', action='store_true')
    checking = commands.add_parser('check', help='静态检查生成的 OpenFOAM 算例')
    checking.add_argument('input', type=Path)
    balancing = commands.add_parser('balance', help='核验求解后的单固体散热及能量平衡')
    balancing.add_argument('input', type=Path)
    balancing.add_argument('--report', type=Path)
    flow = commands.add_parser('flow', help='以求解后的 phi 核对流道及原生壁面热流')
    flow.add_argument('input', type=Path)
    flow.add_argument('--region')
    flow.add_argument('--inlet')
    flow.add_argument('--outlet')
    flow.add_argument('--relative-tolerance', type=float, default=.001)
    flow.add_argument('--wall-heat-flux', metavar='FUNCTION')
    flow.add_argument('--wall-field', default='wallHeatFlux')
    flow.add_argument('--energy-balance', action='store_true', help='对账流道功率、流固界面热量和进出口显热')
    flow.add_argument('--include-kinetic', action='store_true', help='按实际速度和通量计入进出口动能输运')
    flow.add_argument('--energy-tolerance', type=float, default=.02)
    flow.add_argument('--report', type=Path)
    network = commands.add_parser('two-r', help='用原生区域温度回算 2R 结温与两条热支路')
    network.add_argument('input', type=Path)
    network.add_argument('--relative-tolerance', type=float, default=.02)
    network.add_argument('--report', type=Path)
    install=commands.add_parser('install-helper',help='校验并安装 OpenFOAM FloTHERM 辅助库')
    install.add_argument('input',type=Path)
    install.add_argument('--library-dir',type=Path)
    rollback=commands.add_parser('rollback-helper',help='切回指定摘要的已安装辅助库')
    rollback.add_argument('library')
    rollback.add_argument('sha256')
    rollback.add_argument('--library-dir',type=Path)
    args = parser.parse_args(arguments)
    try:
        if args.command == 'inspect':
            supplements = [part for part in (args.pdml, args.ecxml, args.xtxml) if part]
            if args.input.is_dir() or args.input.suffix.lower() == '.json':
                if supplements:
                    raise ValueError('IR 预检不能附加原始文件')
                result = validate(args.input)
            else:
                result = inspect_inputs([args.input, *supplements], studio=False)
        elif args.command == 'validate':
            result = validate(args.input)
        elif args.command == 'export':
            from .flotherm_export import export_pack

            result = export_pack(args.input, args.output, launcher_path=args.flotherm_bat,
                                 force=args.force, keep_stage=args.keep_stage, publish=publish_staged_case)
        elif args.command == 'import':
            inputs = [part for part in (args.input, args.pdml, args.ecxml, args.xtxml) if part]
            result = import_model(inputs, args.config, args.output, args.force)
        elif args.command == 'build':
            result = build(args.input, args.config, args.output, args.force)
        elif args.command == 'check':
            result = check(args.input)
        elif args.command == 'flow':
            from .flotherm_channel import evaluate

            result = _write_report(evaluate(_ordinary_output(args.input), region=args.region, inlet=args.inlet,
                outlet=args.outlet, relative_tolerance=args.relative_tolerance, wall_heat_flux=args.wall_heat_flux,
                wall_field=args.wall_field, energy_balance=args.energy_balance,
                energy_tolerance=args.energy_tolerance, include_kinetic=args.include_kinetic), args.report)
        elif args.command == 'two-r':
            from .flotherm_two_r_results import evaluate

            result = _write_report(evaluate(_ordinary_output(args.input),args.relative_tolerance),args.report)
        elif args.command in {'install-helper','rollback-helper'}:
            from .flotherm_helper_install import activate

            result=(activate(args.input.name,args.library_dir,source=args.input) if args.command=='install-helper'
                    else activate(args.library,args.library_dir,version=args.sha256))
        else:
            result = balance(args.input, args.report)
    except (OSError, ValueError, OverflowError) as error:
        parser.exit(2, f'flotherm2foam: error: {error}\n')
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['ok'] else 2
