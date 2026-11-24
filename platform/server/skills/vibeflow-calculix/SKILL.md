---
name: vibeflow-calculix
description: CalculiX 开源有限元求解器（ccx）的执行契约：.inp 输入卡组、网格来源、求解与 FRD 输出。当算例后端选 CalculiX（结构/热/耦合场）时使用。
---

# CalculiX 契约（自有重写版）

**支持范围**　Windows 和 Linux 已实测托管静力、FRD/日志及版本化发布。使用算例「作业与日志 → 开源求解」选择 .inp，或调用 calculix:solve。高级物理与并行组合以已验收算例为限。

## 输入

- CalculiX 吃 Abaqus 风格 `.inp` 卡组（*NODE/*ELEMENT/*MATERIAL/*BOUNDARY/*STEP…）。
- 网格可来自 gmsh（`gmsh -3 -format inp`）或 hand-written 小模型；单元选型（C3D8 六面体/C3D10 四面体二阶等）写进 `*ELEMENT` 的 TYPE。
- 求解目录放 `C-run/calculix/case-N/`。

## 运行

- `ccx jobname`（读 `jobname.inp`，产 `jobname.frd` 结果 + `jobname.dat` 摘要 + `jobname.sta` 收敛状态）。
- 非线性/接触问题看 `.sta` 的迭代收敛；发散先降载荷步长（`*STEP` 里 `INC`/`DT0` 或自动步长参数）。
- 检查 `.dat` 里的反力/应变能汇总做 sanity check。

## 输出与后处理

- 从求解成果下载 `.frd` 原件。可用专用 FRD 工具读取，或转换为受支持的 VTU 再打开。文本摘要和残差从 `.dat`、`.sta`、作业日志核验。
- 关键结果（最大应力/位移、收敛步数）回写 caseDict `postProcessing.artifacts` 并摘要进 `D-post/summary.md`。

## 常见坑

- 自由度约束不足 → 刚体运动奇异（*BOUNDARY 约束三维平动至少消刚体模态）。
- 接触不收敛优先查法向/容差与罚刚度。
- 二阶四面体（C3D10）比一阶（C3D4）精度好得多，应力问题别用一阶。
