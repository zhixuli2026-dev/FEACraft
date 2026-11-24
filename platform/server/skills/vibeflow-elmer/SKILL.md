---
name: vibeflow-elmer
description: Elmer 多物理场 FEM 求解器的执行契约：.sif 输入（Simulation/Body/Material/Boundary/Solver 段）、ElmerGrid 网格、ElmerSolver 命令与 .vtu 产物。当算例后端选 Elmer 时使用。
---

# Elmer 契约（自有重写版）

**支持范围**　Windows 串行 ElmerSolver 已实测 .sif 与目录网格、VTU/.result 发布及续算。通过 elmer:solve 或开源求解作业提交。MPI 示例需独立运行环境。

## 输入

- 主输入 `.sif`（Solver Input File）：`Header`（网格路径）+ `Simulation` + `Constants` + `Body 1 { Equation; Material; Body Force }` + `Boundary Condition` + `Solver 1 { Equation; Linear Solver; … }` 分段。
- 网格：Elmer 原生目录网格（`ElmerGrid 14 2 mesh.msh` 从 gmsh 转，产 mesh.boundary/header/nodes/elements 四件套）。
- 目录放 `C-run/elmer/case-N/`。

## 运行

- 托管作业封存 .sif 和 Elmer 网格文件，原生执行 `ElmerSolver case.sif`。在算例里用相对路径声明 Mesh DB。并行命令需匹配已安装的 MPI 版。
- 残差/收敛在 stdout；结果 `*.vtu`（**VTU 平台可视化原生可读**）+ `*.dat`（SaveData 的监控量）。
- 多求解器耦合（热-力-流）靠 Equation 段串多个 Solver。

## 常见坑

- `Linear System Solver = Iterative` 时预条件（`ILU0` 等）必填，否则不收敛。
- 坐标/单位 SI 一致；`$` 引用参数可用但别和 caseDict 脱节——参数回写 caseDict。
- 边界名（Body/Boundary 编号）与 ElmerGrid 转换后的编号要对着网格文件核。
