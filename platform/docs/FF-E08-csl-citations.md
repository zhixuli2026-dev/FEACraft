# FF-E08 引文与出版商样式排版

工作台「公式调色板 → 引用」可用 APA 7、IEEE、Chicago 作者年份排版正文引文和书目。点击本地条目的「预览」可排版单条，同一书目内有两至一百条时可用「预览书目」查看编号和年份消歧。在线条目也可以先预览，再收录和插入。BibTeX 继续使用 `doi`、`eprint`、`archivePrefix`，arXiv 同时保存公开链接，Crossref 保存投稿容器、卷期、页码、出版者和完整作者。 [文献 API](../server/app/routers/cite.py) · [引用界面](../web/src/panels/CitationPanel.tsx)

「自定义 CSL」支持 UTF-8 的独立样式和有依赖的出版商样式。选择有依赖的样式后，同时选择指定的独立父样式。若样式指定语言，排版继承该语言。Pandoc 使用固定的 citeproc 参数和本地输入，在用户所属算例的请求范围内运行。学术检索服务断开时，已保存的 `refs.bib` 和本地样式仍可排版。需要可达的工作台 API 和已配置的 Pandoc。样式在当前算例的本次预览中使用，LaTeX 编译样式由正文和编译配方指定。 [预览服务](../server/app/services/citation_format.py) · [Pandoc 引用手册](https://pandoc.org/MANUAL.html#citations) · [CSL 独立与依赖样式](https://docs.citationstyles.org/en/v1.0.2/specification.html) · [预置样式来源与许可](../server/app/services/citation_styles/README.md)

## 实际验收

2026-11-19 在隔离 SQLite、实际文件系统、Edge 157 和 Pandoc 3.9.0.2 验证。APA 同作者同年两条显示 `2022a` 和 `2022b`，读回的 DOI 和 arXiv 均为实际链接。IEEE 显示编号和期刊卷页，Chicago 显示作者年份及完整作者。真实 Nature 独立样式和 Nature Communications 依赖样式产生上标及书目。另用独立脚注 CSL 验证斜体和正文链接。暗色窄栏核对链接色、文件选择器及底部错误反馈，整个面板无横向溢出。 [Edge 和 Pandoc 回执](../../tmp/citation-csl-e08-auvsya6p/citation-csl-result.json) · [真实界面](../../tmp/citation-csl-e08-auvsya6p/publisher-citation-panel.png) · [Nature CSL](https://github.com/citation-style-language/styles/blob/12174e29a13a413a6a1eb53530eb1e54f4896fe5/nature.csl) · [Nature Communications CSL](https://github.com/citation-style-language/styles/blob/12174e29a13a413a6a1eb53530eb1e54f4896fe5/dependent/nature-communications.csl)

隔离断开 Crossref 请求后，界面显示 HTTP 502，同时仍可本地排版。缺少父样式、错误父样式、损坏 XML 及缺失引用键收到 422，更换有效文件后恢复。异主账号返回 404，原书目按 SHA-256 复核保持原字节。七次 Pandoc 子执行各有实际清理证明，临时输入目录均已移除，模型请求与页面异常均为 0。第一次 API 试验发现既有数据库的请求类别约束，随后复用原 `report.prepare` 日志类别和独立 citeproc 用途。 [首次数据库回执](../../tmp/citation-csl-e08-6n5yganc/citation-csl-result.json) · [已登记复验入口](../server/citation_csl_browser_acceptance.py)
