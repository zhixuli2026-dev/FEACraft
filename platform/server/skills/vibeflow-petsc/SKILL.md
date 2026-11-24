---
name: vibeflow-petsc
description: PETSc 可移植科学计算库（线性/非线性求解器后端）的执行契约：KSP/PC 选项体系、命令行选项数据库、与其它框架的关系。当需要定制求解器或调底层求解参数时使用。（自配环境参考）
---

# PETSc 契约（自有重写版）

**支持范围**　自配环境参考。WSL 已实际运行 PETSc 3.19.6 的 CG/Jacobi 离散泊松算例。为发行版 Python 配置当前实数构建的 `PETSC_DIR`，读取实际残差和 CSV。

## 形态

- PETSc 是**库**不是应用：多数时候你通过别的框架碰它（FEniCS/deal.II/自研代码）。自研 C 求解器放 `UDF/`（wmake 体系外，按 PETSc 的 Makefile 模板链）。
- **一切参数走命令行选项数据库**：`-ksp_type cg -pc_type gamg -ksp_rtol 1e-8`——不用改代码就能换求解器/预条件，这是 PETSc 的核心思想。

## 常用选项

- KSP：`-ksp_type cg|gmres|fgmres|bcgs`；收敛 `-ksp_rtol/-ksp_atol/-ksp_max_it`；监视 `-ksp_monitor`。
- PC：`-pc_type jacobi|ilu|gamg|hypre`（hypre 要链 boomeramg）；磁场/块系统用 `-fieldsplit_*`。
- 非线性：`-snes_type newtonls|newtontr`、`-snes_linesearch_type`；失败先看 `-snes_monitor`。
- 性能：`-log_view` 出阶段耗时表（调优必读）。

## 常见坑

- 对称正定别上 GMRES——CG+GAMG 才是正解。
- ILU 在并行下默认变块 ILU（结果随进程数变）——复现性要求时显式说明。
- 选项只在 `PetscOptionsSetValue` 生效顺序里后者覆盖；框架默认值常被命令行覆盖。
- 与平台的衔接：KSP 监视输出进作业日志（残差曲线可读）；矩阵市场格式可导入导出给 postprocess。
