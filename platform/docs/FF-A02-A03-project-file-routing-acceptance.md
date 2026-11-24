# Project 文件树与文件打开

文件树、搜索结果和文档内相对路径链接共用打开入口。普通文本和 Markdown 在编辑器打开。图片、表格、PDF、HTML 与 DOCX 进入查看器。STEP/STP 进入 CAD 预览。Gmsh 网格先转换为 VTU，再进入可视化。VTK 家族按选定文件启动查看，`.foam` 按所属求解目录启动。[路由入口](../web/src/shell/openProjectFile.ts)

请求关联当前用户、算例和本次文件选择。切换算例、关闭标签或打开另一文件后，较早的响应不能重开标签或抢占面板。STEP 解析失败保留当前算例内上一次可用场景。Gmsh 转换失败提供重试和打开原文件。过期的可视化启动只会关闭该次请求创建的会话，当前窗口仍使用自己的会话。[打开状态](../web/src/store.ts) · [CAD 面板](../web/src/panels/GeoPanel.tsx) · [可视化面板](../web/src/panels/VizPanel.tsx)

Open Editors 支持键盘选择、关闭当前标签、全部保存及全部关闭。重新点击同一算例保留打开的标签和草稿。批量保存按顺序提交原始文件版本，避免两份草稿并发争用同一账户的存储额度。单文件失败保持草稿，失败文件可单独重试。关闭含草稿的标签需要确认。复制存储路径取自经过授权的算例接口，共享成员和文件所有者取得同一个逻辑路径。加入聊天会追加文件引用并保留先前草稿。[编辑器](../web/src/panels/EditorPanel.tsx) · [算例接口](../server/app/routers/workspaces.py)

## 2026-11-19 验收

在隔离 API 使用实际 PostgreSQL、SQLite、本地磁盘和 S3 兼容对象存储。Playwright 打开构建后的 Web 界面，保存后通过真实文件 API 回读两份草稿，并检查共享查看者取得所有者路径、未共享账户被拒绝、查看者不能写入。并发测试在真实 HTTP 返回后暂缓交付浏览器，验证先选文本、跨算例 Gmsh 响应都无法覆盖新选择。[验收入口](../server/live_llm_project_file_acceptance.py) · [前端构建](../../tmp/project-file-routing-20261119/web-build-release.log)

| 数据库和文件存储 | 浏览器 | 浏览器证据 | 实际模型回读 |
| --- | --- | --- | --- |
| SQLite 和文件系统 | Chromium | 文件树、编辑、文档、CAD、Gmsh、直接 VTU 和根目录 OpenFOAM 的真实 trame 画布。[界面与接口](../../tmp/live-cli-startup-lqkouqur/project-file-result.json) | 2 次 DeepSeek HTTP 200。[回执](../../tmp/live-cli-startup-lqkouqur/live-result.json) |
| PostgreSQL 和文件系统 | Edge | 文件树、编辑、文档、CAD 和网格转换异常。[界面与接口](../../tmp/live-cli-startup-bx31j7th/project-file-result.json) | 2 次 DeepSeek HTTP 200。[回执](../../tmp/live-cli-startup-bx31j7th/live-result.json) |
| SQLite 和对象存储 | Chromium | 文件树、编辑、文档、CAD 和网格转换异常。[界面与接口](../../tmp/live-cli-startup-4c5ra9og/project-file-result.json) | 2 次 DeepSeek HTTP 200。[回执](../../tmp/live-cli-startup-4c5ra9og/live-result.json) |
| PostgreSQL 和对象存储 | Edge | 文件树、编辑、文档、CAD、Gmsh、直接 VTU 和根目录 OpenFOAM 的真实 trame 画布。[界面与接口](../../tmp/live-cli-startup-7rh041zj/project-file-result.json) | 2 次 DeepSeek HTTP 200。[回执](../../tmp/live-cli-startup-7rh041zj/live-result.json) |

上述部署共记录 8 次真实 `deepseek-flash` HTTP 200，OpenFOAM 标记复验另记录 2 次，模拟调用为 0。模型读回和浏览器文件操作分别记录。各 API 均已退出，真实渲染的会话通过 API 关闭。FF-A02 和 FF-A03 已验收。[标记复验模型回执](../../tmp/live-cli-startup-o2058u9n/live-result.json)

Gmsh 失败时，文件树顶部保持错误和恢复操作可见。STEP 失败保留原可用场景。Impeccable 对文件树、编辑器、CAD 和可视化面板的机械扫描无输出条目。[实际失败截图](../../tmp/live-cli-startup-wo7np0fk/mesh-error.png) · [扫描输出](../../tmp/project-file-routing-20261119/impeccable-detect.json)

此前的通用结果阅读器回归逐格式验证 VTK、VTU、VTP、VTM、XDMF、Exodus、STL 和 series，并在浏览器实际打开 VTP。本轮直接 VTU 使用相同的文件树路由。根目录 `.foam` 使用现有标记或根据源算例编号生成固定标记。实际 VTK OpenFOAM reader 读取四面体得到 1 个单元、4 个点。PostgreSQL 与对象存储复验三类画布并关闭会话后，文件树仍只列出原 `preview.foam`。[逐格式基线](result-readers.md) · [标记与网格核验](../../tmp/project-file-routing-20261119/foam-marker-result.json) · [对象存储复验](../../tmp/live-cli-startup-o2058u9n/project-file-result.json)

复现过批量保存的存储额度结算冲突，以及 Gmsh 转换后启动和复核可视化时的同类冲突。批量保存按顺序提交。可视化在登记和输入复核复用现有的有界准入。保留原持久会话登记、输入指纹与算例授权。[批量保存](../web/src/store.ts) · [可视化准入](../server/app/routers/viz.py)
