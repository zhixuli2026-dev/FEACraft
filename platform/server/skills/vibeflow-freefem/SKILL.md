---
name: vibeflow-freefem
description: FreeFEM 有限元脚本式求解器的执行契约：.edp 脚本结构、网格/有限元空间/变分问题、FreeFem++ 命令与产物。当算例后端选 FreeFEM（自定义弱形式 PDE）时使用。（自配环境参考）
---

# FreeFEM 契约（自有重写版）

**支持范围**　自配环境参考。WSL 已实际运行 FreeFem++ 4.13 的二维 P2 泊松算例，核对三角单元、网格及剖面 CSV。通过算例作业使用 `FreeFem++ -nw poisson.edp`。并行任务需相应 MPI 构建。

## 形态

- FreeFEM 是脚本驱动：`.edp` 文件（`mesh`/`fespace`/`problem|varf`/`solve`）。脚本放 `C-run/freefem/case-N/xxx.edp`，走作业系统（装了才可用）。
- 网格：内建 `square`/分段边界 `border`+`buildmesh`，或 gmsh 出 `.msh` 读入。

## 运行

- 命令：`FreeFem++ xxx.edp`（Windows 有官方版/WSL apt 可得；`ff-mpirun -np N` 并行版）。
- 出图/数据：脚本内 `plot(...)` 在无头环境别用（写 `ofstream`/VTK 输出代替）；结果写 CSV/VTK 后由 postprocess.py/可视化接续。

## 骨架要点

1. `mesh Th = ...` → `fespace Vh(Th, P1)`（或 P2/RT0 等）
2. 变分：`varf a(u,v) = int2d(Th)(...)` + `problem` 或 `matrix`+`solve`
3. 自适应网格：`adaptmesh`（指标驱动）

## 常见坑

- 边界编号（`border` 的 label）与边界条件对应错是最高频 bug——画网格 label 图先验证。
- P1 单元算通量边界导数精度差，要 P2 或恢复技术。
- 大矩阵内存爆 → 换迭代求解器（`solver=CG/GMRES`+预条件）别硬刚 LU。
