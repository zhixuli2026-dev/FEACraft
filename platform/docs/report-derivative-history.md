# 草稿、页码 PDF 与 PPT 的历史入口

2026-11-06，N2-07 第三批。新增 caseDict 报告区的“草稿、页码 PDF 与 PPT 历史”，接通两类生成草稿、页码 PDF、PPTX 的列表、选择和原件下载。这里记录实际实现和验证范围；N2-07 的整项结论见 [验收审计](N2-07-acceptance-audit.md)。

## 用户行为

按类型筛选历史记录，每页 20 条，可继续加载更早版本。记录显示输出路径、该通道内的版本号和发布状态；未成功发布的记录不可选择。选择时重新核验所有权、发布清单、原件摘要和来源状态。当前发布有效、当前发布已过期与历史发布明确区分；没有把“最新生成”当作“当前有效”。

草稿直接展示所选版本只读文本，可以下载原稿以及生成前保存的精确备份。页码 PDF 使用所选版本的原字节预览和下载。PPTX 提供可编辑原件下载，界面明确提示在演示文稿软件中查看；本批没有声称实现 PPT 浏览器渲染器、模板美化或视频能力。

修改工作区草稿/产物不会修改保存的历史原件。已有 PPT 生成按钮旁的下载也固定到该作业的 publication ID，不能因为生成后有人编辑文件而下载错误版本。历史版本读取失败或摘要不符时明确报错，不回退到工作区文件。切换类型或算例会移除旧选择，取消旧版本请求；迟到结果不能进入新算例。

列表刷新失败保留已有条目和当前阅读内容；重试保留原来的刷新/翻页动作。查看和下载均不写回工作区。

## 接口契约

- `GET report/derivatives?family=draft|pdf-number|ppt`：省略 family 返回所有三类；稳定的 `(created_at DESC, id DESC)` 游标，`before_id` 必须属于同算例和筛选范围，`limit` 为 1..100，默认 50。
- `GET report/derivative?publication_id=...`：返回核验后的类型、输出路径、发布状态及可公开下载的原件/备份路径、大小、SHA-256。其他发布类型或其他算例的 ID 返回 404；失败、缺失或损坏的发布证据不能作为原件读取。
- `GET/HEAD report/derivative-artifact?publication_id=...&path=...`：只允许上一个接口列出的公开产物；支持 Range 与 `X-Vibeflow-File-Version`，拒绝版本不符和私有执行元数据路径。前端再次核对实际响应大小和摘要。
- 原 `GET report/compositions` 保持数组响应，补充可选 output 筛选和 before_id/limit 分页，不再只能取前 50 条。

原件下载沿用发布、文件事务和所有权协议。2026-11-19 起，GET 和 HEAD 直接在所选归档版本上分块校验，Range 仅暂存请求范围的响应字节。以下大文件验收记录了实际源读取、响应字节和预留额度。

## 验证证据

| 检查 | 结果 | 具体范围 |
| --- | --- | --- |
| report_derivative_history_smoke.py | 5 项通过 | 64 条同时间戳失败元数据；超过 50 条分页、草稿通道/类型/参数隔离、同所有者跨算例和不同所有者拒绝、失败发布不可读 |
| report_derivative_native_smoke.py | 本地 6 项通过 | 8 个真实草稿/PDF/PPT 发布代次和两版 Pandoc HTML/DOCX；原别名、精确备份、来源/别名修改后历史仍可读、完整下载/HEAD/Range/拒绝错误版本 |
| 同一原生测试 --s3 | 8 项通过 | 同样的实际产物，并从另一个 API 的独立缓存读取所有代次，核对摘要及过期状态 |
| playwright-derivative-history.js | 8 项通过 | 两类草稿、备份、PDF 预览字节、PPT 原件、刷新错误、历史缺失不回退、摘要篡改拒绝，以及既有 PPT 生成下载固定版本 |
| playwright-derivative-history-races.js | 2 项通过 | 禁用失败记录、翻页重试保留游标并追加真实旧记录、切换算例后的迟到响应取消、新算例空状态 |

分页浏览器首屏是 20 条拦截的失败元数据，下一页读取真实 API；实际超过 50 条的数据库行为由 HTTP 测试证明。浏览器 PDF 预览核对 Blob 与原件字节相同，不声称检验浏览器内置 PDF 插件的每个渲染功能。新后端测试使用 SQLite，本批不冒充 PostgreSQL 全矩阵或跨主机验收。

前两次原生测试未通过，均为测试假设错误：PPT 标题是具名可编辑文本框，并非模板标题占位符；修改生成草稿后活动指针会撤销，因此当前标记应为 false。通过实际 PPT 对象和只读查询隔离数据库确认后修正断言，本地及 S3 完整重跑通过；失败日志保留。第一次 detector 参数误用了 CDP 的 --target，未执行扫描；按位置参数扫描所有变更的 UI 文件后结果为空。

生产构建、1440×1000 与 1100×800 实际截图已检查，新增历史区域无横向溢出。原 PPT 表单在基线截图中已有横向溢出，本批没有将它作为新区域的验收结果。独立界面审查单独记录于证据目录。

规范证据：`tmp/mainline-N2-report-history-closure-evidence/`。测试仅使用隔离算例、数据库和 UI 服务；不迁移开发库或重载 8000 服务。

## 2026-11-19 原件下载资源回归

`GET/HEAD /api/v1/cases/{case_id}/report/derivative-artifact` 在算例协调期间绑定指定归档、同算例成功的生产作业与清单摘要，随后释放数据库事务。下载在现有有界工作线程内校验整个归档。编号 PDF 和 PPTX 核验制作通道及公开输出，草稿额外解析并校验结果与工具链 JSON，只公开生成的 Markdown 和该代记载的上版备份。版本头沿用原件路径、发布 ID、清单摘要和原件摘要的算法。HEAD 和 Range 在完整 SHA 校验后再发送响应。 [路由](../server/app/routers/reports.py) · [归档选择](../server/app/services/report_history.py)

已通过公开分块上传和 Windows 原生编号作业生成 41,944,185 B 的可解析历史 PDF。原版和修复版都读取了全部原件用于摘要校验。HTTP 完整、HEAD 和末尾 Range 实际 body 分别为 41,944,185、0 和 97 B。下表是各次读取观测区间的 Python `tracemalloc` 峰值，单位为字节。

| 环境 | 完整 GET | HEAD | 末尾 Range | 暂停原件读取时的独立 SQLite 写事务 |
| --- | ---: | ---: | ---: | --- |
| 原版 SQLite / 文件系统 | 41,959,316 | 41,959,316 | 41,959,316 | 取锁失败 |
| 修复版 SQLite / 文件系统 | 8,924,064 | 151,944 | 185,110 | 可开始，正常同算例文件保存成功 |
| 修复版 SQLite / 认证 SeaweedFS | 1,325,486 | 4,056,108 | 223,850 | 可开始，正常同算例文件保存成功 |

修复版每次以不超过 65,536 B 的块读取单个所选原件。完整 GET 精确预留 41,944,185 B，HEAD 和 97 B Range 的磁盘预留均为零，响应完成后节点预留回到零。两个后端均保留历史与当前代的独立内容、200/206/416、版本不符 409、未认证 401、异主与私有路径 404。同长度损坏发生在 Range 之外时，Range 和 HEAD 均拒绝返回原件。恢复原对象后，同一 Range 再次返回 206 和完全相同的 97 B。两个后端也通过真实 PPTX HEAD 及草稿原件、精确上一版和私有元数据拒绝。每个后端四个原生作业的停止回执已验证，根进程退出码均为 0。 [旧版回执](../../tmp/derivative-artifact-fs-pohg4vhv/report-derivative-artifact-streaming.json) · [修复版文件系统](../../tmp/derivative-artifact-fs-agti0ywd/report-derivative-artifact-streaming.json) · [修复版 SeaweedFS](../../tmp/derivative-artifact-s3-l605nyul/report-derivative-artifact-streaming.json) · [最终执行器](../../tmp/vibeflow-regression-all-20261119-180428-6c5981/summary.json)

首次认证 SeaweedFS 的 16 卷隔离实例，在原件恢复后的正常校验请求中报告没有可写或空闲卷，相关 `PutObject` 返回 `InternalError`。原现场已保留。隔离实例改为 24 个 32 MiB 卷后，只重跑认证 SeaweedFS，完整通过。独立检查确认所有验收 API 及两次 SeaweedFS 自有进程和监听端口均已退出。C 盘可用 21.62 → 21.61 GiB，D 盘可用 380.96 → 378.78 GiB，产物与日志保存在 D 盘。11 个项目 Docker 容器持续运行，构建缓存为 0 B。模型调用为 0。 [首次 SeaweedFS 执行器](../../tmp/vibeflow-regression-all-20261119-175923-9188f5/summary.json) · [卷分配错误](../../tmp/s3-attempt-store-gjwx4rzr/weed.log#L322) · [最终存储回执](../../tmp/s3-attempt-store-0ydurr65/store.json)

本次覆盖原件 HTTP 入口。历史元数据选择、正式制作作业和浏览器侧完整 SHA 校验各自沿用现有路径。

## 后续责任

历史报告入口的范围包含 HTML/DOCX、LaTeX/PDF（前两批）和本批三类产物。完整工作流页面和事件展示继续归 N6；存储层有界读取和性能归 N4；跨主机、多副本归 N7/FF-J14/FF-J19；PPT 全部专业功能、所有模板和高级编辑器仍由 295 项全功能计划逐项交付。当前 PDF 页码按钮的输入仍是工作区文件，历史输入再加工能力不能由历史查看验收代替。
