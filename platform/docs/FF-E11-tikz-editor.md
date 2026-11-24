# TikZ 图形编辑器

FF-E11 为 Web 工作台的新增扩展。原 0.11.0 已删除独立 TikZ 可视入口。新的编辑器位于算例 `caseDict` 的 LaTeX 编译区。 [原版审计](feature-gap-audit.md) · [交付计划](full-function-delivery-plan.md)

画布支持选中和拖动节点、添加矩形或圆形节点以及连接箭头。也可用坐标字段调整节点。拖动和字段编辑只改写对应的 `\node` 行，源码编辑区可继续编辑完整 LaTeX。画布读取第一个 `tikzpicture` 里的标准格式节点与连接。其他命令和注释保留在源码，实际 PDF 使用全量源码编译。

点击「保存并选择编译」把文档存入 `E-report/latex/diagrams/`，并选择为 LaTeX 主文件。已存在图形需先读取，保存使用原文件 SHA-256 对照版本。编译使用已注册的 `latexmk` 作业，当前主文件的输出从对应子目录读取。 [LaTeX 作业和发布](latex-workflows.md)

Edge 实际拖动节点、增加第三个节点与箭头、重新打开并保存同一源码，保留原注释。非特权 WSL 中真实 pdfLaTeX 作业生成 PDF，Poppler 读回两个更名后的标签并渲染页面。首轮复验发现顶层输出列表遗漏子目录结果，已通过按主文件查询输出解决。实际模型 0 次，模拟 0 次。 [验收](../../tmp/ff-e11-tikz-edge-67gfs6c5/tikz-edge.json) · [编辑画布](../../tmp/ff-e11-tikz-edge-67gfs6c5/tikz-canvas.png) · [实际 PDF](../../tmp/ff-e11-tikz-edge-67gfs6c5/tikz-evidence.pdf)
