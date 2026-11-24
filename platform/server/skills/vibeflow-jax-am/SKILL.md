---
name: vibeflow-jax-am
description: JAX-AM 增材制造热过程模拟（移动热源传热、熔池流动及相场）的执行契约。当算例走增材制造热模拟时使用。（自配环境参考）
---

# JAX-AM 契约（自有重写版）

**支持范围**　自配环境参考。执行前在选定运行时确认与输入匹配的 JAX-AM、JAX-FEM 及数值后端，并以相应版本的手册和实际命令结果为准。

JAX-AM 0.0.3 与 JAX 0.8.2 的 CPU 验证使用上游 `jax_am.cfd.cfd_am.AM_3d`。参照 `examples/jax-am/surface_heating.py` 在独立目录运行，启用 `JAX_PLATFORMS=cpu`。依赖还包括 meshio、PyYAML、matplotlib 与 pandas。样例核对无热源恒温、加热温升和 VTU 单元场。

## 形态

- JAX-AM 使用专门的 CFD、相场和有限元模块。按所用物理模型选择入口。
- 将 Python 脚本、材料参数和扫描路径放在独立算例目录 `C-run/jaxam/case-N/`，确保数值输出落在该目录内。

## 要点

- 热源：高斯面热源/体热源（Goldak）+ 扫描路径（`scan path` 序列）——功率/速度/光斑直径是主参数。
- 时间步与激光速度配套（光斑走过一个单元的时间量级）。
- 可微分特性做工艺参数反演（功率/速度→熔池尺寸目标）。

## 常见坑

- 相变潜热处理（表观热容法）参数错→熔池尺寸全错。
- 移动热源时间步必须解出光斑运动（Courant 类约束）。
- GPU 显存：精细熔池网格×长时间序列吃显存——分层分批输出。
- 产物/监控量进 `D-post/`（熔池尺寸时序 CSV→postprocess.py）。
