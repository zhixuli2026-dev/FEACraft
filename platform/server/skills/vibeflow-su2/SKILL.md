---
name: vibeflow-su2
description: SU2 开源 CFD 求解器的执行契约：.cfg 配置结构、网格格式、求解命令、收敛历史与输出产物。当算例后端选 SU2 时使用。
---

# SU2 契约（自有重写版）

**支持范围**　Windows 和 Linux 已实测 SU2_CFD、历史曲线、流场与续算。托管 su2:solve 提交 .cfg 与算例内网格。MPI 示例依赖匹配的构建和环境。

## 配置与网格

- SU2 用单个 `.cfg` 文本配置（键=值，如 `SOLVER= RANS`、`MACH_NUMBER= 0.5`）。
- 网格格式 `.su2`（NDIME/NELEM/NPOIN 结构）；OpenFOAM 网格需转换，gmsh 可出。
- 配置必须与 caseDict 对齐（caseDict 仍是唯一参数源）；求解目录放 `C-run/su2/case-N/`（非 OpenFOAM 后端加一层后端目录）。

## 运行与发布

- 串行：`SU2_CFD xxx.cfg`；并行：`mpirun -np N SU2_CFD xxx.cfg`（需另配并行编译版，托管验收使用当前已验证的配置）。
- 收敛历史在 `history.csv`（或终端表）；残差判据看 `RES_*` 列。
- 产物：`flow.vtk`/`surface_flow.vtk`（可直接进平台可视化/查看器）、`restart.dat`。
- CFG 注释完整模板可让 agent 生成——字段多，先按物理问题（马赫数/雷诺数/湍流模型/边界 MARKER 组）填关键键。

## 常见坑

- `MARKER_*` 边界名必须与网格里的名字逐字符一致（最常见报错源）。
- 非稳态要 `TIME_MARCHING= YES` + `TIME_STEP/MAX_TIME/INNER_ITER` 三件套。
- 残差不降先看 CFL（`CFL_NUMBER`）与网格质量。
