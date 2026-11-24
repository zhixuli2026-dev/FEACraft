---
name: vibeflow-scilab
description: Scilab 数值计算环境的执行契约：.sce/.sci 脚本、无头批处理命令、Xcos 界限与产物。当需要 Scilab 数值后处理/控制计算时使用。（自配环境参考）
---

# Scilab 契约（自有重写版）

**支持范围**　自配环境参考。WSL 已实际运行 Scilab 2024.0.0 的无头 ODE 任务并核对结果 CSV。Ubuntu 24.04 的启动脚本需 `dpkg-dev`，使用 `env HOME="$PWD" scilab-cli -nb -f decay.sce`。

## 形态

- 脚本 `.sce`（立即执行）/函数库 `.sci`（`getf`/`exec` 载入）。放 `C-run/scilab/` 或 `D-post/`（后处理计算脚本）。
- 无头批处理：`scilab -nwni -nb -f xxx.sce`（Windows/WSL 均可）——输出走 stdout/文件。

## 常见用法

- 数据后处理：读 C-run 的 CSV（`csvRead`/`read`）→ 计算（矩阵运算原生强）→ `csvWrite` 出结果给 postprocess.py 出图。
- ODE/控制仿真：`ode`/`csim` 系列。

## 常见坑

- `-nwni -nb` 必带（无窗口不初始化 banner），否则批处理挂起。
- 路径分隔符跨平台统一用 `/`；`get_absolute_file_path` 解析相对路径。
- 大矩阵内存：Scilab 单精度/稀疏矩阵（`sparse`）能省则省。
- 产物回写 caseDict `postProcessing.artifacts`；中间数据落 `D-post/data/`。
