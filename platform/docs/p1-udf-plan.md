# P1-1 UDF D1–D5 垂直切片计划（2026-10-24）

> 状态（2026-10-25）：**最小 functionObject 垂直切片已完成并通过实际验证**；通用 UDF 阶段工作流和其他 UDF 类型不在本次完成范围内。

> 目标：用一个最小但真实的 `functionObject` 证明 D1 需求、D2 设计、D3 实现、D4 编译验证、D5 算例集成。不是只验证安装包或编译成功。

## 范围与验收

- D1：`server/examples/udf/README.md` 明确输入/输出、非目标和验收标准。
- D2：同文档说明类、接口、运行、构建和验证设计。
- D3：`server/examples/udf/myFunctionObject.{H,C}` 与 `Make/{files,options}`。
- D4：`wmake libso` 编译、`ctypes.CDLL` 动态加载。
- D5：把库加入隔离算例 `system/controlDict` 的 `functions`，运行 `icoFoam`，日志出现 `P0_UDF_OK`，并产生新时刻目录。

## 实施与验证

1. 先实现源码和 `udf_smoke.py`，运行编译/加载/集成测试。
2. 若注册失败，检查 `defineTypeNameAndDebug` 与 `addToRunTimeSelectionTable`，不绕过错误。
3. 若加载失败，检查 `LIB_LIBS` 和 `LD_LIBRARY_PATH`，不把 `libNULL.so` 当产物。
4. 集成失败时保留日志，区分编译、链接、动态注册、case 配置和求解错误。
5. 通过后更新 `feature-gap-audit.md`、`parity-checklist.md`、`mechanism-audit.md`、`progress.md` 和测试索引。

## 完成证据（2026-10-25）

- `server/udf_smoke.py` 从清理后的 `D:/tmp/vibeflow-udf-g5` 复制源码与隔离算例，依次通过 `wmake libso`、`ctypes.CDLL` 和 `icoFoam` 集成执行。
- `functions/myFunctionObject` 显式配置 `type myFunctionObject;` 与 `libs ("libmyFunctionObject.so");`；求解日志出现一次 `P0_UDF_OK`，进程正常结束，并生成 `0.1/U`。
- 排错确认 WSL 调用必须使用 `--exec bash -lc`。旧调用让默认 shell 在 source v2406 前展开 `$LD_LIBRARY_PATH` 和命令替换，造成 1912/v2406 运行库混用；修正后 `command -v icoFoam` 指向 v2406，完整切片通过。
- 本项只记最小 G5 垂直切片通过，不把“通用 UDF 工作流、所有 G5 类型或数值对拍”写成完成。

## 边界

- 该切片只证明一种 functionObject 的端到端集成，不代表自定义求解器、湍流模型、边界条件、fvOption、MPI 或其他 UDF 类型全部完成。
- 不把 `UDF/README.md` 模板或技能文本当成 D1–D5 已完成；只有实际编译、加载和算例日志才作为证据。
- 生成物放临时目录，不覆盖用户 UDF 源码或现有 case。
