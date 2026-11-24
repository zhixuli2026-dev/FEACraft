---
name: vibeflow-viz
description: 在 Web 版可视化会话里用脚本驱动 3D 场景（切片/等值线/流线/矢量箭头/表面染色/截图）。当需要把 C-run 求解结果可视化、或向 D-post/scenes.py 写场景脚本时使用。
---

# VibeFlow 可视化场景脚本契约（Web 版）

**支持范围**　实际 VTK/trame 会话已验证算例内场景脚本、截图和原生 VTK、XDMF、Exodus 读取。使用受信任的算例脚本，并以当前会话的日志核验执行。

## 机制

- 在「可视化」面板启动并启用脚本监听后，保存 `D-post/scenes.py` 会在当前会话重新执行。脚本使用当前会话提供的函数。
- 每次执行是**增量语义**：场景里的对象按名字登记；同名对象更新参数（不叠加），新名字才新建。
- **AI 管理的对象一律用 `VF_` 前缀命名**；只改 `VF_*` 对象，互不覆盖。
- 执行日志（含报错）在 `D-post/logs/viz_executor.log`；截图约定输出到 `D-post/figures/`。
- 脚本上下文：以下函数全是**裸函数**（无需 import），脚本自上而下顺序执行，结尾不用手动渲染（平台执行后自动刷新视图；`render()` 也可显式调用）。

## 可用函数（自有 facade）

```python
VF_load("C-run/case-1")        # 载入求解目录为当前数据源；缺省=本会话启动目录。返回 "VF_case"
VF_surface("VF_base", field="p")            # 表面云图
VF_slice("VF_mid", field="Umag", axis="X", pos=0.5)   # 切面：axis X/Y/Z，pos 0~1（沿法向比例）
VF_contour("VF_iso", field="p", iso=0)      # 等值线；iso=0 表示自动取场中值
VF_stream("VF_sl", seeds=40)                # 流线（中心种子线 × seeds）
VF_glyph("VF_vec", scale=1.0)               # 矢量箭头（按 U 定向、模长定大小）
VF_color("VF_base", "p")                    # 改某对象染色场
VF_remove("VF_base")                        # 移除对象
VF_time(0.3)                                # 切到最近的可有时刻并重建全部对象
VF_reset_camera()                           # 取景自适应
VF_shot("figures/vortex.png")               # 截图（相对 D-post/ 的路径）
VF_list()                                   # 当前场景对象名列表
```

- 场名来自求解器输出（OpenFOAM 常见：`p` 压强、`U` 速度向量、`Umag` 速度模长——`Umag` 由平台自动派生）。
- 染色场缺省：有 `Umag` 用 `Umag`，否则第一个标量场。

## 写法约束

- 先想清楚这张图要回答什么问题（对照 caseDict 的 `postProcessing.objectives`），一次脚本把相关对象建全。
- 改图=改脚本：把参数改成想要的再保存即可，同名对象原地更新；要删掉的对象用 `VF_remove`。
- 报错不会弄坏会话：看 `D-post/logs/viz_executor.log` 的 traceback 修正脚本。
- 2D 薄板算例（如 cavity）的"等值面"本质是面上等值线，平台已按此渲染。
- 需要交互查看时提示用户去「可视化」面板手动旋转/缩放；工具栏的显示模式（表面/切面/等值面/流线/矢量箭头）是快速查看通道，与 scenes.py 的对象叠加互不影响。

## 与其它阶段的衔接

- 数值曲线/统计表走 `D-post/postprocess.py`（numpy/matplotlib，作业系统里跑），**不要**把出图塞进 scenes.py。
- 报告引用：截图存 `D-post/figures/` 后在 `E-report/report.md` 里相对引用。
