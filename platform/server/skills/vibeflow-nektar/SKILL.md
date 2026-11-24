---
name: vibeflow-nektar
description: Nektar++ 谱元/hp 元 CFD 框架的执行契约：.xml 会话文件（几何+展开+条件）、求解器命令、场文件产物。当算例后端选 Nektar++（高精度谱方法）时使用。（自配环境参考）
---

# Nektar++ 契约（自有重写版）

**支持范围**　自配 Nektar++。非特权 WSL 已实测 5.10.0 中的 NekMesh、ADRSolver 及 FieldConvert，核对二维 Helmholtz 场及其谱阶收敛。切换运行环境或物理求解器时，先确认对应版本和可执行文件。

## 输入

- 会话单文件 `.xml`：`GEOMETRY`（网格引用或内嵌）+ `EXPANSIONS`（基函数类型/阶数）+ `CONDITIONS`（参数/边界/初值）+ `SOLVERINFO`。
- 网格：gmsh `.msh` 转 `NekMesh in.msh out.xml`；高阶曲面网格配 `Expansion` 阶次。
- 目录放 `C-run/nektar/case-N/`。

## 运行

- 求解器按物理选择 `IncNavierStokesSolver`、`CompressibleFlowSolver` 或 `ADRSolver`。可将网格、展开和边界条件放在同一会话 XML，也可拆成多个参数。
- 使用同一份会话定义转换原生输出，例如 `FieldConvert mesh.xml conditions.xml mesh.fld mesh.vtu`。保留 `.fld` 供原求解器与后续转换使用。
- 时间控制 `TimeStep/NumSteps/IO_CheckSteps`；检查点 `.chk` 续算。

## 常见坑

- 谱元阶数 p 上去后 CFL 骤紧——先低阶跑通再升阶。
- `Expansion` 的 `NUMMODES`（=p+1）写错网格对不上直接炸。
- 启用 MPI 编译后可用 `mpirun -np 2 IncNavierStokesSolver mesh.xml conditions.xml`。并行 `FieldConvert` 的 VTU 默认带 `.pvtu` 索引和分块，保持相对目录结构。 [官方手册](https://doc.nektar.info/userguide/5.10.0/user-guide.html)
