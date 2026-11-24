---
name: vibeflow-openlb
description: OpenLB 格子玻尔兹曼（LBM）求解器的执行契约：C++ 案例骨架（prepareGeometry/prepareLattice/main 循环）、编译与运行、VTK 产物。当算例后端选 OpenLB 时使用。（自配环境参考）
---

# OpenLB 契约（自有重写版）

**支持范围**　自配 OpenLB。已用 GCC 13 在非特权 WSL 构建并实跑官方二维 Poiseuille 算例，核对原生时间序列和中心线速度。其他算例先核对所用发行包的构建选项和算例接口。

## 形态

- OpenLB 是 C++ 框架：每个算例一个 `.cpp`（参考其 examples 骨架：SuperGeometry → prepareLattice（ dynamics/边界条件 ）→ 主循环 getResult/execute）。
- 目录放 `C-run/openlb/case-N/`，配 `Makefile`（链 `olb` 库）。
- 关键参数：格子分辨率（`N`/`resolution`）、松弛时间 τ（= ν·3+0.5 换算）、物理↔格子单位转换（`UnitConverter`）。

## 运行

- 1.9 发行包在 `examples/laminar/poiseuille2d` 执行 `make` 和 `./poiseuille2d`，算例 Makefile 通过 `OLB_ROOT` 引用发行目录。需要 C++20。使用 MPI 前按发行包设置编译器和并行模式。
- `SuperVTMwriter` 输出 `.pvd` 时间索引、`.vtm` 和 `.vti` 数据块，交付时保留相对目录关系。
- 核对日志的阶段迭代、稳定性/收敛记录和误差范数。对于 Poiseuille 流可从实际 `physVelocity` 检查壁面速度及归一化抛物线。

## 常见坑

- τ 接近 0.5 数值不稳；雷诺数上去先加密而不是硬扛。
- 边界条件对象（`addVelocityBoundary`/`addPressureBoundary` 的 superGroup 号）与几何标记错配是最高频坑。
- 单位转换器（UnitConverter）的 physDeltaX/physDeltaT 一旦定错，全场尺度全错——先打印换算表核对。
