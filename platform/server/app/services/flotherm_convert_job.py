"""异步 Flotherm 转换作业入口。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.flotherm_case import generate_case, write_run_scripts
from app.services.flotherm_coverage import write_coverage_report
from app.services.flotherm_ir import validate_ir, write_ir
from app.services.flotherm_output import MARKER, validate_conversion_dirs, write_manifest
from app.services.flotherm_settings import load_settings


def convert(case: Path, ir_path: str, settings: str | None, output_dir: str):
    ir = validate_ir(json.loads((case / ir_path).read_text(encoding="utf-8")))
    if settings:
        ir = load_settings(ir, case / settings)
    output = (case / output_dir).resolve()
    if not output.is_relative_to(case.resolve()):
        raise ValueError("输出目录越过 case")
    validate_conversion_dirs(output, output.parent / f".{output.name}-ir-work")
    manifest = generate_case(ir, output)
    write_run_scripts(output, manifest)
    write_ir(output, ir)
    write_coverage_report(output, ir, manifest)
    write_manifest(output, [p.relative_to(output).as_posix() for p in output.rglob('*')
                           if p.is_file() and p.name != MARKER])
    print("FLOTHERM_CONVERT_OK", json.dumps({"objects": len(ir["objects"]),
          "power_w": ir["coverage"]["power_w"], "output": output_dir}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--ir", required=True)
    parser.add_argument("--settings")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    convert(args.case, args.ir, args.settings, args.output)


if __name__ == "__main__":
    main()
