---
name: vibeflow-openradioss
description: OpenRadioss 显式冲击/碰撞求解器的执行契约：Starter/Engine 两段式、deck 输入、动画/时序产物。当算例走显式动力学（碰撞/爆炸/跌落）时使用。（自配环境参考）
---

# OpenRadioss 契约（自有重写版）

**支持范围**　自配环境参考。执行前在选定运行时确认同版本的 Starter 与 Engine，并以相应版本的手册和实际命令结果为准。

在非特权 WSL 分别核验原 OpenRadioss `latest-20260728` 与社区延续版 OpenCourant `latest-20261011`。两个版本均载入各自的 `hm_cfg_files` 和动态库，使用 `starter_linux64_gf -i case_0000.rad -np 1 -nt 2`、`engine_linux64_gf -i case_0001.rad -nt 2`、`anim_to_vtk_linux64_gf caseA001 > frame.vtk`。记录精确版本与可执行文件来源。

## 形态

- 两段式：`starter`（模型检查+初始化，读 `_0000.rad`）→ `engine`（求解，读 `_0001.rad`）。
- deck 是 Radioss 卡格式（`/NODE /BRICK /PART /MAT /PROP /BC /LOAD`…）；网格可由 HyperMesh/gmsh 转换。
- 目录放 `C-run/openradioss/case-N/`。

## 运行

- `starter -i case_0000.rad`（必须零 ERROR 才放行）→ `engine -i case_0001.rad`（MPI 版加 `-np`）。
- 产物：动画帧 `A00x`（→ 后可转 VTK）、时序 `*.csv`（`TH` 输出：加速度/力/能量）。
- 显式稳定时间步极小（单元最小尺寸决定）——质量缩放（`/DTIX` 或 AMS）要权衡精度。

## 常见坑

- Starter 的 ERROR 必须清零——带错进 engine 必崩。
- 自接触（自撞）漏定义 → 穿透垃圾结果。
- 能量守恒检查：引擎日志的 Energy error 百分比必须小（<5% 经验线）。
- 时序曲线进 postprocess.py；动画帧转平台可视化走 VTK 转换链。
