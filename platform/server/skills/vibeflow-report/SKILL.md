---
name: vibeflow-report
description: E 阶段报告契约：E-report/report.md 的章节结构、证据溯源规则、图片/公式/表格规范、pandoc 构建产物。当撰写或构建仿真报告时使用。
---

# 报告契约（自有重写版）

**支持范围**　Pandoc HTML/DOCX 已实测，构建依赖所选运行时的 Pandoc。报告作业的证据与输出版本独立保留。

## 章节结构（report.md，中文 10 节）

1. 问题定义与目标
2. 范围、假设与验收标准
3. 几何与计算域
4. 网格策略与质量
5. 物理模型、材料与边界条件
6. 数值方法（离散格式/求解器/收敛准则）
7. 收敛性与监视量分析
8. 后处理与结果分析
9. 局限性与不确定度
10. 结论与建议

每节关键数值必须可溯源（注明证据源：checkMesh 日志、残差曲线、D-post/data 的 csv、D-post/figures 的图、caseDict 字段）。

## 写作规则

- 学术文体，作者视角；结论与局限性并列，不回避坏结果。
- 展示公式用 `$$ … $$` 独立成行；行内公式 `$…$`。
- 图片用**相对路径**引用 `../D-post/figures/xxx.png`，并在正文点名每张图回答什么问题。
- 存在未结清的 blocking 错误（caseDict `errors`）时，报告不得标完成。
- 写完把产物路径回写 caseDict 的 `report.outputFile` / `execution.artifacts`。

## 构建（本平台）

- 平台构建：`POST /cases/{id}/report:build`（pandoc → `E-report/build/report.html` + `report.docx`），CasePanel 上有按钮，走作业系统。
- 构建失败先查作业日志（pandoc 对公式/图片路径报错最常见）。
- 报告图先在「查看器」里人工过一遍：标签不裁切、灰度可辨、单位齐全。

## 与可视化的衔接

- 场景图用 scenes.py 的 `VF_shot("figures/xxx.png")` 产出（见 vibeflow-viz 技能）。
- 曲线图用 postprocess.py 产出（见 vibeflow-postprocess 技能）。
- 两类图都要进 `figures/` 并在 summary.md 登记。
