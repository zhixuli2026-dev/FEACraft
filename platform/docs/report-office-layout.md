# 导出 DOCX 的实际办公软件排版与复杂语义阅读

2026-11-17，FF-E02 / FF-E22，WP09 / R4 / T07。此前 reference-doc 的颜色和页边距仅核对输出 XML；本批用实际 LibreOffice 打开用户下载的 DOCX，导出 PDF 并检查页面，也用实际 Chromium 阅读相同发布的 HTML/DOCX。不是从 Markdown 另行生成一份 PDF 代替 DOCX 验收。

## 实际内容和结果

算例30、作业25，输入包含中文与英文、加粗/斜体、嵌套列表、两行代码、脚注、SVG图形、三处公式、章节书签和100行表格。reference-doc 配置标题色 `8C245B`、1440 twip 页边距、页眉和 PAGE 页码域。所有数据为排版算例，不代表工程求解结论。

- 注册 Pandoc 作业成功，模板原件哈希进入输入清单，DOCX 所有 XML/关系部件可解析，根及子进程停止回执已核验。
- LibreOffice `25.2.3.2 520(Build:2)` 从原样下载 DOCX 得到4页 PDF；100个行号全部存在，中文、脚注、图注和结束标记完整，每页有页眉和正确的1–4页码。第1、2、4页实际图像已查看：标题颜色、图形/公式、跨页重复表头、脚注和页边距正常。
- 当前应用的 HTML/DOCX 阅读器各通过1组实际浏览器验收：101个表格行（含表头）、100条数据、嵌套列表、强调、代码换行、3处公式、SVG、脚注、书签定位及下载原件逐字节相同。浏览器未捕获页面错误。
- DOCX 阅读器仍明确为语义阅读；页眉页脚和分页的证据属于导出文档在办公软件中的渲染，不把应用预览描述成 Word 排版复刻。

共4组实际检查（Pandoc生成、LibreOffice排版、HTML语义阅读、DOCX语义阅读），模型调用0、模拟LLM0。PDF SHA-256：`2691bee2f0867a98b3c272b66b2f53a4841b1c182131ce645223ccace7e697ca`。

## 实际发现及修复

1. Mammoth 默认把 Pandoc 的 `Source Code` 样式当普通段落；现在通过既有 styleMap 转成 `pre > code`，两行代码实际换行保留。原 DOCX 字节不变。
2. DOCX 标题书签转换为标题前的空 `<a id="…">`；此前焦点和提示落在不可见空元素。现在遇到紧随其后的标题时将焦点交给该标题，提示使用标题文字；其他空目标至少显示请求的 ID。

最初作业24因测试模板关系文件使用 `ns0` 前缀，触发 Pandoc 3.1 的复制行为并产生无前缀声明的输出，已被生产 XML 校验拒绝。失败目录 `office-layout-fd73252d45` 和损坏原件保留。模板改用 OOXML 常规默认命名空间后作业25通过；没有放宽生产校验。浏览器的代码段/空书签两次失败分别保存为 `browser-result-before-code-style.json`、`browser-result-before-bookmark.json`，不是以修改断言消除失败。

## 运行和证据

入口 `tools/report-office-acceptance.py`（生成/办公软件渲染）和 `tools/report-office-browser.cjs`（实际阅读器）。最终源码审查 `tmp/FF-E02-office-layout-review-v7-20261117.json` 共673份，包含精确 renderer Dockerfile；该版相对实际浏览器通过时 v6 只补入 renderer 文件，没有变更已执行源码。

结果目录 `tmp/docker-deployment-20261117/office-layout-f59d586df8/`：`result.json`、`browser-result.json`、`job.json`、`publication.json`、原件、PDF及页面图像。账号文件保留在 private 子目录，不纳入发布包。

独立渲染镜像 `vibeflow/office-renderer:20261117`，ID `sha256:6a0feea4c21249bc02b8c069666a613cca0b14bab25f8cfea15c4b64555c4898`。LibreOffice Writer/Math 和 Noto CJK、Liberation、DejaVu 字体来自 Debian 包；实际执行 UID65534、`--network none`，只读挂载输入、单独输出目录，容器执行后移除。PDFium `pypdfium2==5.15.2` 只安装在 `tmp/office-render-tools`，用于将实际 PDF 页面转成检查图像，没有添加到生产依赖。

渲染使用 `--headless --convert-to pdf:writer_pdf_Export --outdir` 和独立 `UserInstallation`，参数依据 [LibreOffice 官方启动参数](https://help.libreoffice.org/latest/en-GB/text/shared/guide/start_parameters.html?DbPAR=BASIC&System=WIN) 与 [PDF导出参数](https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html?DbPAR=SHARED)。本次仅证明实际测试版本，不声明所有办公软件、Word版本或任意外部模板的像素一致性。

当前 web 镜像 `vibeflow/web:docx-layout-20261117`，ID `sha256:6b5ea7a392f63b7839c208a765267bde03a83aa45836622adef536f834fce2bb`，TypeScript/Vite正式构建通过。API/worker仍为完整CSS v2。回退web至 `document-anchor-20261117` 可回到本批之前界面，账号和文件无需回滚。

后续依 [具体剩余核对](report-delivery-reconciliation.md) 推进 srcset 和新增资源的S3/worker组合。主线49/53、剩4；资源11/17、剩6；295项目标保持，本批不直接关闭E02/E22整项。
