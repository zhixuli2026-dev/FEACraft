"""I10D：case/IR 输出保护与取消保留部分产物。"""
import json
from pathlib import Path
import tempfile

from app.services.flotherm_output import (CancellationToken, ConversionCancelled,
    prepare_output, validate_conversion_dirs)

with tempfile.TemporaryDirectory(prefix="vf-i10d-") as temp:
    root = Path(temp)
    case = root / "case"
    ir = root / "ir"
    assert validate_conversion_dirs(case, ir) == (case.resolve(), ir.resolve())
    for bad_case, bad_ir in ((case, case), (case, case / "nested"), (case / "nested", case)):
        try:
            validate_conversion_dirs(bad_case, bad_ir)
        except ValueError:
            pass
        else:
            raise AssertionError("相同/嵌套输出未拒绝")
    case.mkdir()
    (case / "existing.txt").write_text("KEEP", encoding="utf-8")
    try:
        validate_conversion_dirs(case, ir)
    except ValueError:
        pass
    else:
        raise AssertionError("非空输出未拒绝")
    (case / "existing.txt").unlink()

    case.mkdir(exist_ok=True)
    partial = case / "system" / "controlDict"
    partial.parent.mkdir()
    partial.write_text("PARTIAL_GENERATED", encoding="utf-8")
    token = CancellationToken()
    token.cancel()
    try:
        token.checkpoint(case, "job-i10d")
    except ConversionCancelled:
        pass
    else:
        raise AssertionError("取消检查点未生效")
    assert partial.read_text() == "PARTIAL_GENERATED"
    cancelled = json.loads((case / "conversion-cancelled.json").read_text())
    assert cancelled["partial_output_preserved"] is True and cancelled["job_id"] == "job-i10d"
    try:
        prepare_output(case, force=False)
    except (ValueError, FileExistsError):
        pass
    else:
        raise AssertionError("取消后同一非空目录不应直接重启")
    assert partial.read_text() == "PARTIAL_GENERATED"
print("I10D distinct/non-nested/empty preflight and cancellation preserves partial output: OK")
print("\n=== I10D Flotherm 输出保护冒烟通过 ===")
