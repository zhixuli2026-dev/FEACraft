# FF-E02 / FF-E22 原交付条件核对

2026-11-18。对应 WP09、R4、T07，依据 [全功能计划第5节及第7节](full-function-delivery-plan.md)。FF-E02 与 FF-E22 已按各自原功能条件验收；保留原 required_work 和全部 295 项目标。此结论不替代 N7-06 的全项目最终门槛。

| 原要求 | 已有实际证据 | 最终逐项结论 |
| --- | --- | --- |
| E02正常生成及完整UI/API | [注册报告](report-workflows.md)、[构建控件](report-build-controls.md)、[Docker实际报告](docker-report-deployment.md)：普通Markdown和工程报告共用注册处理器；实际构建、选择、取消与下载 | [工程模板](report-engineering-templates.md)补齐真实求解到自动正文、可编辑草稿和再次导出；手写稿保持。现有上传及caseDict编辑入口配置，无平行API。已验收。 |
| E02完整模板资源、版本提交 | [DOCX模板](report-reference-template.md)、[CSS资源](report-css-resources.md)、[响应式图片](report-responsive-images.md)以及[S3独立worker组合](report-s3-resources.md)已通过；资源修改、新版与旧原件保持 | 自动工程compose已接入同样的配置、资源封存和版本复核，FS完整工程链与S3重新提取/生成均通过；修改模板后新版本变化、旧原件逐字节保持。已验收。 |
| E02复杂版式实际渲染 | HTML实际Chromium自包含样式/390px媒体查询；DOCX在语义阅读器中实际显示SVG及12处公式；[LibreOffice实际排版](report-office-layout.md)已核验下载DOCX的4页、100行表格、标题色、页眉页码、脚注、公式和图形 | 此项指定文档的真实排版已补证，不再列为仅XML检查；不承诺所有Word版本或任意模板逐像素相同 |
| E22原语义阅读能力 | [固定版本阅读](document-publication-reader.md)、[任意历史](report-history-selection.md)、[公式](docx-math-reader.md)、[章节定位](document-anchor-navigation.md)、[复杂语义组合](report-office-layout.md)和本轮S3实际浏览器已完成 | FF-E22原条件已验收，见[逐项结论](document-reader-acceptance.md)，不再等待E02工程模板 |
| 错误、取消与恢复 | 原实际运行中取消及迟到跨算例响应；S3缺失资源拒绝、排队取消、worker重建、损坏DOCX与实际修复重读通过 | 工程模板非法配置、缺失依赖拒绝及过期检查通过，原生停止凭据核验；既有取消/恢复路径未改动。已验收。 |
| 权限与数据边界 | N2-07原证据；本轮S3真实第二用户5类404及当前源码主动HTML/远程候选过滤通过 | 跨物理主机矩阵仍由N7-01承担；E22此项已完成 |
| 配置/兼容、产物与文档 | Windows Python3.11.7/Pandoc3.9.0.2、Docker Python3.11.18/Pandoc3.1.11.1实际产物；原依赖保持 | 有模板与无模板真实工程流程均通过，完整API镜像部署、源码和失败证据归档；使用与回退见[工程模板文档](report-engineering-templates.md)。已验收，最终N7-06门槛保持。 |

本批汇总：`tmp/mainline-engineering-templates-evidence/validation.json`。本地工程5项、S3工程2项、实际HTML浏览器1项、默认无模板真实闭环1组；18个成功原生作业。此前已经通过且本次未改动的 UI/权限/复杂版式等证据按上表沿用，不冒充本批重跑。普通Markdown的S3后端7项、浏览器3项、读取边界3项仍见[S3验收](report-s3-resources.md)。

精确执行版本分开：工程模板FS使用v5；S3与浏览器使用v6；v7仅修正无模板时空资源解析记录的准备/提交一致性，并运行原默认导热闭环。最终部署源码与v7核对；旧源码、失败和成功证据均保留。

模型证据分开：模板阶段曾实际调用 DeepSeek `deepseek-flash` 一次应用请求并读回报告；本轮CSS、重启下载和章节定位均不涉及模型回复，调用数0。没有mock LLM；历史模型/协议测试不混算为本轮真实模型验收。

全功能严格整项2/295、剩293（FF-E02、FF-E22已验收）；主线49/53（N4-05、N7-01、N7-05、N7-06剩余）、资源11/17剩6。最终N7-06及295项总目标保持。
