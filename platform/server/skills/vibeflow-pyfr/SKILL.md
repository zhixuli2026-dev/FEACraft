---
name: vibeflow-pyfr
description: PyFR 高阶通量重构 CFD 求解器的执行契约：.ini 配置、网格转换、运行命令与解文件产物。当算例后端选 PyFR（高阶/LES/大规模并行）时使用。（自配环境参考）
---

# PyFR 契约（自有重写版）

**支持范围**　自配环境参考。WSL 已实测 PyFR 3.1、MPICH 5.0.1 与 OpenMP。官方二维 Euler 涡旋的导入、三阶求解和 VTU 导出通过。运行环境需包含匹配的 MPI、支持 OpenMP 的 C 编译器及 LIBXSMM。

## 输入

- 配置单文件 `.ini`（`[backend]`/`[constants]`/`[solver]`/`[solver-time-integrator]` 等节）。
- 网格使用 PyFR 的 `.pyfrm`。通过 `pyfr import in.msh out.pyfrm` 导入 Gmsh 网格。
- 目录放 `C-run/pyfr/case-N/`。

## 运行

- 使用 `pyfr run -b openmp mesh.pyfrm config.ini` 执行 CPU 算例，生成 `*.pyfrs`。多分区运行先使用 `pyfr partition add mesh.pyfrm N`。
- 使用 `pyfr export volume mesh.pyfrm soln.pyfrs out.vtu` 导出 VTU，可用 VTK 读取场数据。
- 后端的 OpenMP/CUDA/HIP 选择（`[backend]`）按 WSL/容器里的硬件定。

## 常见坑

- 高阶（order ≥2）对网格畸变敏感——checkMesh 思路不适用，看 L2 残差与熵守恒测试。
- `cfl` 与 `pseudo-niters` 对 stiff 算例要先保守。
- 时间格式：`tstart/tend/dt` 是物理时间；`stats` 输出间隔按步数。

## 对接平台

- 残差/统计在 stdout 与 `.pyfrs`；作业日志曲线照常用；VTU 导出后进可视化面板。
- 关键产物回写 caseDict `postProcessing.artifacts`。
