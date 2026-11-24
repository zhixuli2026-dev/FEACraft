# FF-CB13 工程报告章节、专业模板与图件

工程草稿保留原有九个章节 ID。组稿接口新增版本化的 `chapter_schema`，登记每章的编号、标题、caseDict 来源块、证据类型和图件。工作流直接生成报告时，发布清单的 `engineering_report` 保存同一套章节结构。整文 Markdown 构建沿用源码、资源和草稿发布的关联。

在 caseDict 的 `report` 块配置 `template auto;` 可按 `caseMeta.discipline` 选取结果章模板，按 `caseMeta.analysisType` 选择说明重点。省略模板字段时自动选择，缺少学科时使用通用结果章。支持 `general`、`fluid`、`solid`、`thermal`、`electromagnetic`、`acoustic`、`multiphysics` 和 `custom`。稳态、瞬态、静力、动力、模态、谐响应、优化、参数及不确定度分析均使用各自关注项。表格以实际求解和提取结果为准。 [模板](../server/app/services/report_templates.py) · [章节生成](../server/app/services/report_builder.py)

结果章按后处理发布清单插入已有图件，优先选 PNG，其次选 SVG。图号、标题、字段、单位、来源路径及 SHA-256 随章节记录。PDF 图件可以从证据索引打开。用户可在现有草稿编辑器中修改正文，工作流直接生成的 HTML 和 DOCX 独立发布。

生成草稿和工程报告都封存自动插图、`referenceDoc`、每级 CSS `@import` 及 CSS 引用的图像和字体。构建手写 Markdown 继续从 Pandoc 文档和资源解析器读取完整依赖。资源读取沿用算例范围和现有候选路径规则，缺失资产在准备时拒绝，请求时封存字节在发布时重新核对。历史产物仍可按原发布 ID 下载。 [资源选择](../server/app/services/report_artifacts.py) · [资源解析](../server/app/services/report_build.py)

## 实际验收

- 改动前，真实 80 单元 OpenFOAM 算例及 VTK 后处理产出了同一张温度图。报告 Markdown 的证据索引已有链接，封存记录可定位 PNG，HTML 和 DOCX 内均无插图，草稿响应只有章节 ID。 [旧入口实测](../../tmp/live-cli-startup-hyh88_8v/engineering-report-schema.json)
- 首轮修改加入九章结构和热分析模板，两个公开草稿入口均自动生成温度图。实际 Pandoc 输出的 HTML 和 DOCX 都嵌入图件，DOCX 内图件与实际 PNG 逐字节一致。 [首轮文件系统验收](../../tmp/live-cli-startup-6la1mymt/engineering-report-schema.json)
- SQLite 文件系统用同一个真实工程验证非法模板和缺失资源拒绝。两种草稿都封存实际 DOCX 模板、嵌套 CSS 和 PNG。改动子样式会令已生成版本标记过期，原 HTML 和 DOCX 仍按原发布 ID 下载，逐字节相同。独立注册工程链通过工作流直接生成 HTML/DOCX，清单内的章节和温度图摘要一致。 [草稿与历史](../../tmp/live-cli-startup-2_kto44b/engineering-report-schema.json) · [注册工作流](../../tmp/live-cli-startup-nhrpvwq0/engineering-report-schema.json)
- 最终版正文按页面宽度放置图件。Edge 在 1,300 和 390 像素视口均显示完整的 1,800×1,200 温度图，嵌套样式生效，外部请求为 0。LibreOffice 直接打开下载的 DOCX 并生成三页 PDF，第二页图件和图注齐备。 [最终注册工作流](../../tmp/live-cli-startup-r5vzpaps/engineering-report-schema.json) · [Edge 窄屏](../../tmp/live-cli-startup-r5vzpaps/engineering-report-390-edge.png) · [浏览器回执](../../tmp/live-cli-startup-r5vzpaps/engineering-report-figure-edge.json) · [LibreOffice 回执](../../tmp/cb13-office-r5vzpaps/office-result.json)
- PostgreSQL 搭配认证 SeaweedFS 完成真实导热案例、两种草稿、模板依赖封存、历史回读和最终 HTML/DOCX 编译。两种格式嵌入同一份温度图，改动子样式后读取原报告字节一致。临时数据库和对象存储已清理。 [认证 S3 实测](../../tmp/live-cli-startup-nw_57qus/engineering-report-schema.json) · [数据库清理](../../tmp/job-repeat-postgres-ikle5a1m/result.json) · [对象存储清理](../../tmp/s3-attempt-store-45onrpeb/job-repeat-run.json)
- 专业模板配置核对覆盖七种 caseMeta 学科与九种分析类型的 63 种组合，另验证通用回退和显式选择。这是配置核对，物理解算证据见上述真实热传导案例。 [模板配置结果](../../tmp/engineering-report-template-matrix-20261121.json)
- 既有 CasePanel 生成与保存、跨用户拒绝、上游过期和历史报告读取已有真实 Edge 与原生作业证据。报告工作流的取消和恢复沿用实际发布与原生停止记录。 [报告工作台](FF-E01-report-chapter-schema.md) · [发布权限与恢复](report-delivery-reconciliation.md)

首轮嵌套 CSS 的用例直接在 HTML 字节中查找颜色，样式实际位于自包含的 Base64 导入中。后续用例解码嵌套样式，并用 Edge 核对实际标题颜色。两次认证 S3 并行对照分别在原生命令的提交及回读准备阶段达到客户端的 30 秒等待上限，保留原记录。 [嵌套样式首轮](../../tmp/live-cli-startup-g1gx3k_u/live-result.json) · [S3 对照一](../../tmp/live-cli-startup-sowtgz_v/live-result.json) · [S3 对照二](../../tmp/live-cli-startup-ubaitbnr/live-result.json)

模型回读共 11 次真实 DeepSeek HTTP 200，模拟 0 次。其中一次成功读取文件后，后续请求出现连接错误。最终 S3 文件回读完成。此阶段 C 盘可用空间从 23,652,675,584 变为 23,631,998,976 字节。LibreOffice 复用现有镜像，本轮 Docker 回收量为 0。 [模型和磁盘汇总](../../tmp/engineering-report-schema-validation-20261121.json) · [最终 S3 回读](../../tmp/live-cli-startup-nw_57qus/live-result.json)

## 复跑

```powershell
$python = 'platform/server/.venv/Scripts/python.exe'
& $python -B -X utf8 platform/server/live_llm_engineering_report_schema_acceptance.py --source-profile-id 26
& $python -B -X utf8 platform/server/live_llm_engineering_report_schema_acceptance.py --source-profile-id 26 --postgres --s3
& $python -B -X utf8 platform/server/live_llm_engineering_report_schema_acceptance.py --source-profile-id 26 --registered
& 'C:/ProgramData/anaconda3/python.exe' -B -X utf8 platform/server/live_llm_engineering_report_schema_acceptance.py --browser-report tmp/live-cli-startup-r5vzpaps/schema-report.html
```
