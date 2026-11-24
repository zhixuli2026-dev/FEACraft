"""Local FloTHERM input inspection and OpenFOAM case generation."""
import argparse
import json
from pathlib import Path
import sys

from app.services.flotherm_output import VERSION, prepare_output


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {'inspect','export','import','validate','build','check','balance','flow','two-r',
                                           'install-helper','rollback-helper'}:
        from app.services.flotherm_cli import run
        raise SystemExit(run(sys.argv[1:]))
    parser = argparse.ArgumentParser(prog="flotherm2foam")
    parser.epilog = '子命令：inspect、export、import、validate、build、check、balance、flow、two-r、install-helper、rollback-helper。使用 <子命令> --help 查看参数。'
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-stage", action="store_true")
    parser.add_argument('--inspect', type=Path, nargs='+', metavar='INPUT', help='检查输入组合并输出 JSON')
    args = parser.parse_args()
    if args.version:
        print(f"flotherm2foam {VERSION}")
        return
    if args.keep_stage:
        parser.error("--keep-stage 适用于 export 子命令")
    if args.inspect is not None:
        from app.services.flotherm_inputs import inspect_inputs
        if args.output is not None or args.force:
            parser.error('--inspect 只读取输入文件，不能与 --output 或 --force 一起使用')
        try:
            result = inspect_inputs(args.inspect, studio=False)
        except (ValueError, OSError) as error:
            parser.exit(2, f'flotherm2foam: error: {error}\n')
        print(json.dumps(result, ensure_ascii=True))
        if not result['ok']:
            parser.exit(2)
        return
    if args.output is None:
        parser.error("--output 为必填项（--version 除外）")
    try:
        prepare_output(args.output, force=args.force)
    except (ValueError, FileExistsError, OSError) as error:
        parser.exit(2, f"flotherm2foam: error: {error}\n")
    print(f"FLOTHERM_OUTPUT_READY {args.output.resolve()}")


if __name__ == "__main__":
    main()
