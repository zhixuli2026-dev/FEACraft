---
name: vibeflow-laserbeamfoam
description: laserbeamFoam（OpenFOAM 系激光熔化/焊接求解器）的执行契约：OpenFOAM 字典生态+激光源参数。当算例走激光增材/焊接熔池 CFD 时使用。（自配环境参考）
---

# laserbeamFoam 契约（自有重写版）

**支持范围**　自配环境参考。执行前在选定运行时确认与其源码版本匹配的 OpenFOAM 及 laserbeamFoam 可执行文件，并以相应版本的手册和实际命令结果为准。

## 形态

- OpenFOAM 生态求解器（基于 interFoam 系多相流 + 激光热源 + 熔融/凝固）：目录结构与 OpenFOAM 全同（0/constant/system）。
- V3.1 使用区分大小写的 `constant/LaserProperties`。官方 `Plate2D` 同时读取 `constant/timeVsLaserPosition*` 与 `constant/timeVsLaserPower*`。
- 目录放 `C-run/laser/case-N/`；前置工作流全走 vibeflow-openfoam 契约（网格/字典/作业）。

## 运行

- 在验证过的 WSL 环境载入 OpenFOAM v2506，执行 V3.1 源码根目录的 `./Allwmake`。在算例目录内依次执行 `blockMesh`、`setFields`、`laserbeamFoam`。`Plate2D` 先将随附的 `initial` 复制为 `0`。
- 多相 VOF 追踪金属与周围相的界面。`alpha.metal` 是金属体积分数。可用 `foamToVTK` 导出温度 `T`、速度 `U` 和 `alpha.metal`，从 `VTKs/` 读取求解器直接写出的射线序列。

## 常见坑

- 激光路径函数（扫描线/螺旋）与域尺寸配合，先空跑路径可视化再开热源。
- 网格分辨率：光斑直径至少 10 单元——熔池梯度剧烈。
- Marangoni 项（表面张力梯度）不稳定时先关它调试基线。
- 全链路监控/后处理与 vibeflow-openfoam + vibeflow-postprocess 契约一致。
