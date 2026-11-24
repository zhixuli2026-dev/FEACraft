---
name: vibeflow-lammps
description: LAMMPS 分子动力学求解器的执行契约：in 脚本结构、data 文件、求解命令、热力学输出与轨迹产物。当算例后端选 LAMMPS（分子/颗粒/粗粒化）时使用。（自配环境参考）
---

# LAMMPS 契约（自有重写版）

**支持范围**　自配环境参考。WSL 已实际运行 LAMMPS 2024 双粒子键能量极小化，核对轨迹及键长。通过算例作业使用 `lmp -in relax-dimer.in`，执行前按输入样式确认对应构建。

## 输入

- 入口是 `.in` 脚本（`units/atom_style/read_data` + pair 势 + fix 系综 + run）。
- 构型数据 `.data`（`read_data`）或 `read_restart`；势文件（eam 等）放算例 `Ref/` 或系统势库。
- 目录放 `C-run/lammps/case-N/`。

## 运行

- 串行：`lmp -in xxx.in`；并行：`mpirun -np N lmp -in xxx.in`（WSL 装了才可用）。
- 热力学输出（thermo 行）进 stdout → 作业日志里可看能量/温度轨迹；`log.lammps` 落盘。
- 轨迹：`dump ... atom/lammpstrj/custom` 出 `dump.*` 文件（后处理用 OVITO 或自解析）。

## 常见坑

- `units`（lj/real/metal）与势参数体系必须配套——错配是 MD 最常见翻车源。
- 丢原子（lost atoms）= 结构爆炸：先降 timestep/升温速率，查初始重叠。
- `pair_style` 参数顺序与势文件段落对应错会静默出垃圾结果——能量守恒系综验证（NVE 能量漂移检查）必须有。
- 结果回写 caseDict `postProcessing.artifacts`；热力学曲线用 postprocess.py 出图（见 vibeflow-postprocess）。
