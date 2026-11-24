# DOCX 样式模板与容器地址恢复

2026-11-17：报告构建支持算例内 `reference-doc`，模板与正文、图片一起冻结和复核。实际 Pandoc 验收验证标题颜色、页边距、模板变更、历史下载和错误输入；真实 DeepSeek 工具读取报告通过。另修复 API 容器替换后 nginx 缓存旧地址而持续返回 502 的问题。

## 使用 DOCX 模板

上传模板到算例的 `E-report/templates/brand reference.docx`，在 `E-report/report.md` 开头加入：

```yaml
---
reference-doc: "templates/brand reference.docx"
---
```

然后使用已有“构建 HTML / DOCX”按钮。路径按报告目录优先、算例根目录其次的既有资源规则解析，支持文件名中的空格。模板必须位于算例内；远程 URI、带查询参数/片段的路径及非 DOCX 文件拒绝构建。

模板提供 Word 样式和页面设置，其正文不会替换报告 Markdown。HTML 输出继续使用 HTML 的排版规则，DOCX 模板不冒充 HTML 样式表。模板原件和解析选择记录进入既有来源清单；独立重编译与发布前复核继续生效。修改模板后，旧报告来源被标记过期；新构建使用新模板，指定旧发布仍可下载当时原件。

原生 Pandoc 调用新增可选 `reference_doc` 字段，只允许 DOCX 编译时传入当前冻结工作目录内的 DOCX。未使用模板的旧调用结构保持兼容，没有开放任意命令参数。

模板与报告的所有 `.xml`、`.rels` 部件均需可解析。实际发现 Pandoc 3.1.11.1 处理某些非惯用命名空间前缀的模板时会生成损坏的 `styles.xml`，原校验仅解析正文而漏过；现补齐检查，无法生成有效文件时明确失败。验收模板采用常规 Word 的 `w` 前缀，不宣称已修复 Pandoc 自身对任意前缀的兼容性。使用 Word/Pandoc 产生的正常模板；失败时调整模板后重新构建，保留旧发布。

## 实际证据

入口 `platform/server/live_llm_report_template_acceptance.py`；审查登记 `tmp/FF-E02-reference-doc-review-v4-20261117.json`，665 份源码归档。最终 [result.json](../../tmp/docker-deployment-20261117/reference-doc-e929615c24/result.json) 的 6 项验收通过：

1. 缺失模板和非 ZIP 模板被拒绝，不创建作业。
2. ZIP 中损坏的样式 XML 被拒绝，不创建作业。
3. 真实作业 19 采用模板标题色 `8C245B`、左边距 1877 twips；模板哈希进入冻结清单。
4. 模板改为 `17634B`、2333 twips 后旧来源过期，真实作业 20 生成新版本；两个版本原件各自保持。
5. 远程模板被拒绝，已有成功发布保持。
6. 真实 DeepSeek `deepseek-flash` 调用读取生成的 HTML 并返回未知验证串，工具结果持久化。应用请求 1 次，上游 HTTP 次数未计量，模拟 LLM 0、Claude 0。

两个作业的根停止证明均通过。模板颜色和页边距是输出 DOCX XML 的实际值；本轮没有用 Word/LibreOffice 做像素级版式验收，不能据此宣称复杂布局全部通过。之前的浏览器公式验收是独立证据。

保留失败目录：`reference-doc-530d33ab28`（API 替换后的旧代理地址 502）、`27d951f8dc`（测试基底无页边距元素）、`28982a63d8`（测试 XML 声明编码错误）、`4be0039207`（非惯用模板前缀导致输出样式 XML 损坏）、`1df1654fb0`（API 尚未就绪时发起测试）。这些失败均在模型调用之前；不计入成功，也未用模拟响应替换。

## nginx 自动刷新后端地址

`infra/nginx-web.conf` 使用 Docker 内部 DNS `127.0.0.11`、5 秒有效期、共享 upstream zone 和 `resolve`。要求 nginx 开源版不低于 1.27.3；当前镜像实际为 1.27.5。行为依据 [nginx upstream resolve 文档](https://nginx.org/en/docs/http/ngx_http_upstream_module.html#resolve)。

实际入口 `platform/tools/docker-upstream-acceptance.py`，审查 `tmp/N7-05-dynamic-upstream-review-v2-20261117.json`。在没有活动作业时替换 API 容器，并用只运行 sleep 的临时容器占住空闲地址，确保地址实际变化；临时容器已移除。最终 [result.json](../../tmp/docker-deployment-20261117/upstream-11b1060125/result.json) 记录：

- API 从 `172.18.0.4` 变为 `172.18.0.7`，代理观察到短暂 502 后约 2.51 秒恢复 HTTP 200。
- nginx 容器及 worker PID 保持，期间没有 restart/reload。
- 重新登录后带查询参数下载两个历史 DOCX，SHA-256 与替换前完全一致。

这是实际基础设施验收，模型调用 0。首次尝试使用显式 Docker IP，被旧版自动子网策略拒绝；API 已由 finally 恢复，后续采用正常地址分配并保留真实地址变化断言。上述 2.51 秒是该次观测，不是无停机或固定恢复时限承诺。

## 剩余工作

后续已接入本地 CSS、嵌套导入、字体及图片冻结，并完成独立 HTML 的实际渲染与版本验证，见 [CSS 与引用资源](report-css-resources.md)。完整复杂布局、跨存储/权限/恢复组合和全功能验收仍保留，FF-E02 不整项关闭。主线仍 49/53、剩 4；资源 11/17、剩 6；295 项全功能目标不变。
