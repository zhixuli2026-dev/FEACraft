---
name: vibeflow-moose
description: MOOSE 多物理场框架（INL）的执行契约：.i 输入块结构、App 派生、命令与 ExodusII 产物。当算例后端选 MOOSE（核/岩土/相场等强耦合多物理）时使用。
---

# MOOSE 契约（自有重写版）

**支持范围**　已实测 Windows API 经非特权 WSL 运行 MOOSE 串行扩散与瞬态续算，发布 CSV、Exodus 和检查点。托管 moose:solve 使用单文件 .i 与受支持的输入段。

## 输入

- 单文件 `.i`（块结构：`[Mesh]/[Variables]/[Kernels]/[BCs]/[Materials]/[Executioner]/[Outputs]`）。
- 托管输入用 `GeneratedMesh`。外部网格、外部 include 和动态模块需独立 MOOSE 环境。
- 求解器在 `[Executioner]`（Steady/Transient + PETSc 选项直通 `-ksp_*`——MOOSE 底层是 PETSc，见 vibeflow-petsc）。
- 目录放 `C-run/moose/case-N/`。

## 运行

- 已验收的串行命令为 `moose-opt -i xxx.i`。平台提交时提供 `input_path`，可见发布以输入名生成 CSV 和 Exodus。
- 产物 `out*.e`（ExodusII）+ CSV（postprocessor）。
- 从算例文件树直接打开 `.e`、`.exo`、`.exodus`。可视化会话从文件读取时刻和字段。瞬态续算使用前一发布的检查点。

## 常见坑

- `[Kernels]` 的 `variable` 名与 `[Variables]` 声明对不上是最高频错。
- 材料属性段忘记挂到 `[Materials]` 对应块——静默用默认值。
- Transient 的 `dt` 与 `num_steps` 配合；自适应开 `[Adaptivity]`。
- 强耦合不收敛：先 `line_search` + 降 `nl_rel_tol` 目标逐步紧。
