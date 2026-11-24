# OpenFOAM v2406 字典补全与来源

Monaco 根据光标前的字典键和当前文件名请求值补全，支持换行后的值、嵌套字段及注释后的值。索引中有该键但缺少当前文件的例子时，编辑器继续查询同一个键。服务不可用时，编辑器使用内置常用字典键和值。切换文件或编辑内容后，已过期的候选不会应用到新内容。候选按服务端证据排序，同名内置项让位给包含置信度和源码路径的服务端项。[Monaco 补全](../web/src/utils/openfoamLang.ts) · [索引 API](../server/app/routers/completion.py)

服务端按字典键与值读取对应的文件证据，避免将不同键下观察到的同名值误判为当前文件的候选。例如，`U` 文件中的 `uniform` 属于 `internalField` 或其它值字段的证据，仅凭值在 `U` 出现不足以将其用于 `type`。指定文件名时返回该文件的一条源码路径。没有索引或索引 JSON 损坏时返回 `ready: false`，恢复文件后重新读取。API 需要登录，返回所有账号共用的静态 OpenFOAM 数据。[查询实现](../server/app/services/completion.py)

## 静态索引

使用官方 v2406 源码归档独立构建两次。两个结果排除时间戳后完全一致，保留基准的 31,542 个去重标签与原分类。解析有界读取 gzip 教程文件，将 27,781 条历史乱码降为零。旧索引的 2,895 条注册包含 1,446 条 `lnInclude` 别名，已逐条核对同名源码注册。发布索引统计为 1,449 条独立源码注册、13 个非空运行时分类、275 个复合键、18 种 `FoamFile` class 和 428 个命令行标记。[来源和校验值](../server/data/completion/PROVENANCE.md) · [两次构建的比对结果](../../tmp/openfoam-completion-20261119/index-verification.json)

从官方归档提取 `src`、`applications`、`tutorials`，在保留大小写的 Linux 目录运行 `completion_builder.py <source-root> <source-index.json> "OpenCFD OpenFOAM v2406 official release archive"`。随后在 `platform/server` 使用 `python -m app.services.completion_merge <existing-baseline.json> <source-index.json> <new-index.json>` 生成候选。比较 `baseline.sha256`、`index_sha256`、标签集和配套验收结果后部署 `openfoam-keywords.json`。部署通过重启 API 读取新索引，回退时恢复上一版索引和前端构建并重启。旧索引备份已保留。[构建器](../server/app/services/completion_builder.py) · [合并入口](../server/app/services/completion_merge.py) · [旧索引](../../tmp/openfoam-completion-20261119/previous-openfoam-keywords.json)

## 2026-11-19 验收

四种组合各通过四组浏览器场景。以 Ctrl+Space 完成跨行的 `writeControl timeStep`，切换 `U`、`V` 后按文件名匹配值，在候选详情中读到实际教程路径。未知文件名使用同一字典键的候选，补全接口断开后可继续插入内置值。最终构建从仓库的 `package-lock.json` 安装并使用 `npm run build`，交付验收使用这份前端产物和已核验索引。[专项入口](../server/live_llm_foam_completion_acceptance.py) · [锁定依赖安装](../../tmp/openfoam-completion-20261119/npm-locked-install.log) · [前端构建](../../tmp/openfoam-completion-20261119/web-build-locked.log)

实际 API 通过独立账号、匿名请求拒绝、参数长度和返回条数上限、四个并发读者的首次加载、索引缺失及损坏后的恢复，以及原分类和文件证据。`fixedGradient` 在 `V` 中的源码例子由官方归档直接读回。十二类生成器断言、上下文和置信度冒烟、基准精确集合回归通过。[分类回归入口](../server/completion_categories_smoke.py) · [上下文回归入口](../server/completion_domain_smoke.py) · [来源回归入口](../server/completion_provenance_smoke.py) · [基准回归入口](../server/completion_baseline_smoke.py)

| 部署 | 浏览器 | 场景、权限与来源 | 模型读取 |
| --- | --- | --- | --- |
| SQLite 与文件系统 | Chromium | [回执](../../tmp/live-cli-startup-j3f02i97/foam-completion.json) · [候选详情](../../tmp/live-cli-startup-j3f02i97/foam-context.png) | [回执](../../tmp/live-cli-startup-j3f02i97/live-result.json) |
| PostgreSQL 与文件系统 | Edge | [回执](../../tmp/live-cli-startup-6iq7xsih/foam-completion.json) | [回执](../../tmp/live-cli-startup-6iq7xsih/live-result.json) |
| SQLite 与 SeaweedFS S3 | Chromium | [回执](../../tmp/live-cli-startup-hiwxj909/foam-completion.json) | [回执](../../tmp/live-cli-startup-hiwxj909/live-result.json) |
| PostgreSQL 与 SeaweedFS S3 | Edge | [回执](../../tmp/live-cli-startup-ut79k_vp/foam-completion.json) | [回执](../../tmp/live-cli-startup-ut79k_vp/live-result.json) |

四组的验收摘要由真实 `deepseek-flash` 使用文件工具精确回读，最终 8 次 HTTP 200，模拟 0。四个 API 和两套 S3 实例已退出，两座临时 PostgreSQL 数据库已删除。较早的四组成功轮次使用了非锁定版本的前端依赖，另有 8 次真实 HTTP 200，整阶段合计 16 次。早期验收探针曾使用错误分类、旧编辑器选择器和 `.foam` 可视化路径，使用实际编辑器和字段文件后复跑通过。[分类初测](../../tmp/live-cli-startup-qze093d2/live-result.json) · [浏览器初测](../../tmp/openfoam-completion-20261119/fs-sqlite-r2.log) · [S3 和 PostgreSQL 回收](../../tmp/job-repeat-postgres-g2nsrl0m/result.json)
