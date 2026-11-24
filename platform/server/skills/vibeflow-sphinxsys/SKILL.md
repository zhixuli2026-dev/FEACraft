---
name: vibeflow-sphinxsys
description: SPHinXsys（C++ SPH 框架，耦合流固）的执行契约：案例类派生、粒子生成、执行器链。当算例走 SPH 流固耦合/复杂自由表面时使用。（自配环境参考）
---

# SPHinXsys 契约（自有重写版）

**支持范围**　自配环境参考。执行前在选定运行时确认SPHinXsys 对应版本、C++ 构建工具与案例程序，并以相应版本的手册和实际命令结果为准。

## 形态

- C++ 框架：每算例一个 .cpp（`SPHSystem`+`BodyWithParticles` 派生类+`Inner/Contact/Boundary` 关系+执行器链 `Dynamics1Level`…）。
- 目录放 `C-run/sphinxsys/case-N/` + Makefile/CMake（链库）。
- `v1.5_update_sycl_2000` 的二维 CPU 库可随官方 `test_2d_dambreak` 用 CMake 构建。算例保留随附的参考 `input/`，在算例目录执行 `test_2d_dambreak --state_recording=true --regression=false`，用现有参考数据核验压力和机械能。
- `BodyStatesRecordingToVtp` 输出 `output/WaterBody_*.vtp` 粒子帧，包括 `OriginalID`、坐标和速度。观察点压力及总机械能各自保存为 `output/*.dat`。

## 要点

- 粒子间距 dp 即分辨率（同 DualSPHysics 经验）。
- 流固耦合：固体 body 用 `SolidParticles`+本构模型；接触用 `ContactRelation`。
- 时间步靠 `TimeStepping` 执行器的声学 CFL 自动控制。

## 常见坑

- 粒子初始分布与几何穿模（生成时检查 body 包围盒覆盖）。
- 弱可压 WCSPH 的人工声速（至少 10× 最大流速）——压力震荡先查它。
- 与 DualSPHysics 选型：要脚本化快速建模选 DualSPHysics，要自定义耦合物理选 SPHinXsys。
