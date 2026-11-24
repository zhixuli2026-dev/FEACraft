---
name: vibeflow-jax-fem
description: JAX-FEM（GPU 可微分有限元）的执行契约：纯 Python 问题定义（网格/弱形式/求解）、jax 加速与可微特性、XDMF 产物。当需要可微分 FEM（反问题/优化）时使用。（自配环境参考）
---

# JAX-FEM 契约（自有重写版）

**支持范围**　自配环境参考。WSL CPU 已实际验证 JAX-FEM 0.0.12 和 JAX 0.8.2。样例 `poisson_sensitivity.py` 求解泊松方程，比较伴随梯度和独立中心差分，并输出 VTU。GPU 后端使用相应的设备和依赖。

## 形态

- 纯 Python：准备网格，定义 `Problem` 子类的 `get_tensor_map`、`get_mass_map` 和所需边界项，再调用 `solver` 或 `ad_wrapper`。
- 放 `C-run/jaxfem/case-N/solve.py`；走作业系统（WSL GPU 环境装了才可用；CPU 也能跑）。

## 卖点与用法

- **可微分**：通过 `set_params` 绑定目标参数，使用 `ad_wrapper` 后通过 `jax.grad` 求解灵敏度。先用有限差分核对对应参数和目标。
- JIT 编译首次慢（XLA 编译）属正常；批量参数研究时缓存编译。
- 产物：`jax_fem` 的 `sol` → meshio 写 XDMF/VTU → 平台可视化/后处理。

## 常见坑

- float64 默认关：`jax.config.update("jax_enable_x64", True)` 工程计算建议开。
- GPU 显存爆 → 降网格或关即时编译缓存。
- 与 FEniCS 选型：要成熟生态选 FEniCS（vibeflow-fenics），要可微分/GPU 选 JAX-FEM。
