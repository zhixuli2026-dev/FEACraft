# FF-E01 工程报告章节与证据

2026-11-19。本项对应 WP09、T07 的报告章节与 schema。已有实际报告源码列出九个章节 ID，但正文和 DOCX 只有八个二级标题。假设与几何、网格与方法各被合并，另有一节执行信息没有自己的 schema ID。网页根据返回列表显示「9 章」，与用户打开的正文不一致。[原审计](feature-gap-audit.md) · [旧实际报告源码](../../tmp/cae-registered-conduction-73cpb5qu/downloaded-report.source-6.md)

现在每写入一个二级标题就登记其章节 ID。九章依次为问题与验收、假设、几何与材料、网格与质量、数值方法及执行、后处理结果、Verification 与 Validation、不确定度及限制、证据索引。问题章读取 `intent.userIntent`。工程执行信息归入数值方法章。Pandoc 构建的 HTML 使用这九个固定 ID 作为章锚点，原 Markdown 仍可编辑。[生成器](../server/app/services/report_builder.py) · [Pandoc 标题属性](https://pandoc.org/MANUAL.html#extension-header_attributes)

## 实际验收

- 80 单元稳态导热算例经过实际 CadQuery、OpenFOAM 网格和求解、VTK 提取及 Pandoc 3.9.0.2。逐单元按实际网格中心复核 `T(x)=300+100x`，最大误差为 0 K。后处理报告均温约 350 K，配置的数值分辨率预算 `0.2 K × 2` 作为扩展不确定度写进 0.4 K 表格。工程 verification 通过，实验 validation 的状态为未验证。[真实工程与报告回执](../../tmp/e01-engineering-report-6ohatomw/benchmark.json)
- 工作流自动报告和用户编辑后发布的报告均核验了 Markdown 九个章节 ID、HTML 九个一致的 `h2` 锚点，以及 DOCX 九个 `Heading2`。实际 Edge 从「组织报告章节」确认备份并生成草稿，在 Monaco 条件编辑和保存，完成正常「构建 HTML / DOCX」后打开两种格式。阅读器实际跳转到限制章，两个原件下载的 SHA-256 和发布清单一致。[实际 Edge 回执](../../tmp/e01-engineering-report-6ohatomw/e01-browser.json) · [章节定位与不确定度表](../../tmp/e01-engineering-report-6ohatomw/engineering-report-chapters.png)
- 原手写草稿备份和前一已发布版本逐字节核对。第二账户的报告详情、草稿、产物和生成均被拒绝。修改上游场文件会拒绝新构建，原报告仍可按固定版本读取。最终八个实际作业成功，隔离 API 已退出。独立核对八个作业根及十五个工具子进程的原始宿主身份均已退出。模型调用为 0。[独立回收回执](../../tmp/e01-engineering-report-6ohatomw/cleanup-independent.json) · [工程与历史检查](../../tmp/e01-engineering-report-6ohatomw/benchmark.json)

复验入口复用已审查的真实注册导热工作流，使用独立 SQLite/文件系统 API 和生产前端构建。报告生成器属于后端变更，复验使用既有前端。[复验入口](../server/report_chapters_browser_acceptance.py)

```powershell
& 'platform/server/.venv/Scripts/python.exe' -B -X utf8 `
  'platform/server/report_chapters_browser_acceptance.py' `
  --dist 'tmp/h21-openfoam-editor-dist-20261119' `
  --browser-python 'C:/ProgramData/anaconda3/python.exe' --channel msedge
```
