---
name: vibeflow-PPT
description: PPTX 演示文稿生成契约：从报告/后处理产物组装幻灯片（python-pptx 作业链）。当用户要把仿真结果做成 PPT 汇报时使用。
---

# PPT 生成契约（自有重写版）

**支持范围**　算例面板和 ppt:build 已实测 PPTX 模板、原生图形和表格、图片重建及独立发布。PPTX 旁白视频使用另行配置的 Impress、Poppler、eSpeak NG 和 FFmpeg。

## 形态

- 在算例面板配置演示稿，或提交 `POST /api/v1/cases/{id}/ppt:build`。后台生成独立发布的可下载 PPTX。
- 素材来源：`E-report/report.md` 的章节结构 + `D-post/figures/`（图）+ `D-post/summary.md`（指标表）。
- 算例面板可编辑构建参数、模板、形状与素材。下载后可用兼容的演示文稿应用继续编辑。

## 骨架（python-pptx）

```python
from pptx import Presentation
from pptx.util import Inches, Pt
prs = Presentation()
# 标题页 / 目录 / 每节一页（标题+要点 bullet+图）/ 结论页
```

- 模板优先：`Presentation("template.pptx")`（组织模板放 `Ref/`），别从零造样式。
- 图用 `D-post/figures/` 的 PNG（300dpi+）；表格用 python-pptx 的 table 而不是截图。
- 中文字体显式设（微软雅黑等）；别依赖默认。

## 契约

- 每页备注写证据源（`notes_slide`）——汇报可溯源。
- 产物路径回写 caseDict `report.artifacts`。
- 失败排错看作业日志（python-pptx 对图片格式敏感：用 PNG 不用 SVG）。
