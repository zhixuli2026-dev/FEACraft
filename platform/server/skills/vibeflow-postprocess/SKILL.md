---
name: vibeflow-postprocess
description: D 阶段后处理契约：从 C-run 与 caseDict 提取指标/曲线/场景图/证据表，产出报告级产物。当需要分析求解结果、画图、提指标、写 summary.md 时使用。
---

# 后处理契约（自有重写版）

**支持范围**　已实测从已确认求解版本提取 CSV、PNG 和证据摘要。工作流确认 D 消费实际发布的同版本结果。

## 目标导向

后处理不是堆图：先读 caseDict 的 `postProcessing.objectives.primaryMetrics`，明确每张图/每个表回答什么问题，再动手。

## 数据源优先级（C-run 内）

1. `postProcessing/`（functionObject 输出：forces/probes/sample 等）
2. 求解器日志（`*.log`，平台的日志解析服务可提残差/时间序列）
3. 时刻目录场文件（`0.5/U` 等）
4. `system/` 配置
5. `B-mesh/` 网格质量产物

## 三个脚本的分工（不可混淆）

- **`D-post/postprocess.py`**：numpy/matplotlib 数据分析与出图——在**作业系统里跑**（shell 作业 `python D-post/postprocess.py`），产物落盘。
- **`D-post/scenes.py`**：可视化场景脚本——**写文件即自动执行**进当前可视化会话（见 vibeflow-viz 技能的 VF_* 契约），**禁止手动运行**。
- **`D-post/pvpython.py`**：离线批处理占位（本版暂未接 pvpython，出图优先 postprocess.py + matplotlib）。

## 产物布局

- 数据表 → `D-post/data/*.csv`
- 图 → `D-post/figures/`（矢量 PDF/SVG + 300dpi 以上 PNG 双出）
- 动画 → `D-post/animations/`
- 指标汇总与证据索引 → `D-post/summary.md`
- 关键产物路径回写 caseDict 的 `postProcessing.artifacts`

## 出图规则（报告级）

- matplotlib 的 rcParams 集中配置（字体/线宽/刻度/图例），不在单个 plot 调用里堆临时样式。
- 坐标轴标签必须含 物理量+符号+单位（如 `t (s)`、`C_D`、`Δp (Pa)`）。
- 顺序标量场用 `viridis`/`cividis`；**禁用 jet/rainbow**；零中心有符号数据用发散色图；多序列用色盲友好调色板+线型/标记双编码。
- 不用平滑掩盖原始波动；滤波要保留原始数据并记录窗口。
- 对数轴要显式处理非正值；坐标轴截断必须披露。
- 每张关键图保留：绘图脚本路径 + 数据 CSV + 数据源，保证可复现。
- 离线脚本开头 `matplotlib.use("Agg")`。

## 收敛性判断

出结论前先确认：末时刻到达、残差下降到位（作业面板残差曲线）、物理监视量（如升阻力）平稳、无未解释的发散或 FOAM FATAL。证据不足时写入 caseDict `errors` 并回到求解阶段，不要硬写结论。
