---
name: vibeflow-fenics
description: FEniCS 有限元框架（Python 脚本式求解器）的执行契约：网格/函数空间/变分形式/求解器参数。当算例后端选 FEniCS（偏微分方程自定义弱形式）时使用。（自配环境参考）
---

# FEniCS 契约（自有重写版）

**支持范围**　自配环境参考。WSL 已实际运行 DOLFINx 0.11.0 的泊松算例，验证两级网格、二阶误差收敛及 XDMF/HDF5 产物。运行算例时选择安装了 DOLFINx 及匹配 PETSc 接口的 Python。

## 形态

- FEniCS 没有"字典文件"——求解脚本就是 Python（`from dolfinx import ...` 或旧版 `from fenics import ...`）。脚本放 `C-run/fenics/case-N/solve.py`，走作业系统（WSL/容器里装了才可用）。
- 网格：内建网格（`RectangleMesh`/`BoxMesh`，dolfinx 为 `mesh.create_box` 等）或 gmsh 出 `.msh` 转 XDMF。
- caseDict 仍管参数源：脚本从 caseDict 读参数（可用 `read_text_file`+解析，或写个小的参数 JSON 同步）。

## 求解骨架

1. 网格 + 函数空间（`FunctionSpace(mesh, "P", 1)` / `VectorElement`）
2. 边界标记 + DirichletBC / 变分弱形式（`ufl`：`dot(grad(u), grad(v))*dx`）
3. 线性/非线性求解器（PETSc 后端；线性给 `solver_parameters` 或用 `petsc_options`）
4. 输出 `XDMFFile`（`xdmf` 可被 ParaView/平台读）

## 常见坑

- dolfinx 与旧版 fenics API 差异大——写脚本前先确认 WSL 里装的是哪个（`python -c "import dolfinx"` vs `import fenics`）。
- 函数空间阶次与网格阶次要配套（二阶网格配一阶空间会退阶）。
- 收敛残差打印（`set_log_level`）进作业日志可监控；发散先查边界条件完备性。
