"""H04：求解日志时刻、Courant、残差、FATAL/Warning 与耗时的确定性 golden。"""
from app.services.logparse import parse_solver_log

primary = parse_solver_log(
    "Time = 0.1\r\n"
    "Courant Number mean: 1.25e-02 max: 5.0e-01\r\n"
    "smoothSolver: Solving for Ux, Initial residual = 1e-1, Final residual = 1e-2, No Iterations 2\r\n"
    "GAMG: Solving for p, Initial residual = 2e-1, Final residual = 2e-2, No Iterations 3\r\n"
    "ExecutionTime = 1.25 s  ClockTime = 2 s\r\n"
    "Time = 2e-1\r\n"
    "PBiCGStab: Solving for Ux, Initial residual = 3e-2, Final residual = 3e-3, No Iterations 4\r\n"
    "ExecutionTime = 2.5 s\r\n"
    "End\r\n"
)
assert primary["times"] == [0.1, 0.2], primary
assert primary["courant"] == [(0.0125, 0.5)], primary
assert primary["residuals"]["Ux"] == [0.01, 0.003], primary
assert primary["residuals_detail"]["p"] == [{"initial": 0.2, "final": 0.02, "iterations": 3}], primary
assert primary["summary"]["last_time"] == 0.2, primary
assert primary["summary"]["execution_time_last_s"] == 2.5, primary
assert primary["summary"]["finished_cleanly"] is True, primary
assert primary["summary"]["fatal_errors"] == 0 and primary["summary"]["warnings"] == 0, primary
print("1. OpenFOAM 时刻/科学计数/Courant/多求解器残差/耗时/CRLF End [OK]")

secondary = parse_solver_log(
    "TimeStep   12: Time = 0.5\n"
    "ExecutionTime = 4.0 s\n"
    "TimeStep   13: Time = 7.5e-1\n"
    "ExecutionTime = 5.5 s\n"
    "Finalising\n"
)
assert secondary["times"] == [0.5, 0.75], secondary
assert secondary["summary"]["last_time"] == 0.75, secondary
assert secondary["summary"]["execution_time_last_s"] == 5.5, secondary
assert secondary["summary"]["finished_cleanly"] is True, secondary
print("2. TimeStep n: Time = t 第二格式 [OK]")

errors = parse_solver_log(
    "  -- Warning: first warning\n"
    "WARNING: second warning\n"
    "--> FOAM FATAL ERROR: first fatal\n"
    "FOAM FATAL IO ERROR: second fatal\n"
)
assert errors["summary"]["warnings"] == 2, errors
assert errors["summary"]["fatal_errors"] == 2, errors
assert errors["summary"]["finished_cleanly"] is False, errors
print("3. FATAL/Warning 计数与未正常结束 [OK]")

print("\n=== 求解日志解析 golden 通过 ===")
