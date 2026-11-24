# FF-A12 项目文件树增量刷新

旧生产构建在跨 API 创建文件后，用 11 次 `GET /tree` 读到新文件，增量请求为 0。用例在检查增量请求时失败，尚未进入模型回读。[旧构建请求记录](../../tmp/live-cli-startup-ck7cdc5m/tree-delta-browser.json)

浏览器以持久事件页为准，对 WebSocket 提示合并请求，按事件游标处理完整路径清单。`POST /tree:patch` 读取指定路径的当前元数据，按本轮返回结果增补、更新或移除行。常规文件变化保持已展开目录和其余行。算例选择、用户和导航代次共同约束应用响应，切换后丢弃上一个算例的迟到结果。[浏览器事件处理](../web/src/caseEventStream.ts) · [文件树状态注入](../web/src/store.ts)

完整树返回与观察同一事务读取的事件水位。遇到旧版或不完整事件、路径预算超限、游标失效或增量读取失败时，浏览器用已有的整树接口恢复，之后继续按持久游标补拉。健康连接继续定期补查事件，断线时保留定期整树检查。增量读取沿用算例读权限、文件协调器和有界读线程。文件系统直接采集请求路径，S3 从当前清单取指定条目。旧 S3 前缀可回退到整树读取。[公开 API](../server/app/routers/files.py) · [路径读取](../server/app/services/case_tree.py)

增量读拒绝其他账户、撤销共享后的旧会话和越界路径。具有只读共享权限的账户可查询路径。使用相同应用版本可以同时服务旧前端，回退前端即可恢复旧刷新逻辑，本次无数据库迁移。[验收入口](../server/live_llm_tree_delta_acceptance.py)

## 浏览器与实际存储

| 数据库与存储 | 四种组合的实际浏览器记录 | 真实模型回执 |
|---|---|---|
| SQLite 与文件系统 | [结果](../../tmp/live-cli-startup-hogjcb23/tree-delta-browser.json) · [增量后的项目树](../../tmp/live-cli-startup-hogjcb23/tree-incremental.png) | [回执](../../tmp/live-cli-startup-hogjcb23/live-result.json) |
| PostgreSQL 与文件系统 | [结果](../../tmp/live-cli-startup-tp_ujupo/tree-delta-browser.json) | [回执](../../tmp/live-cli-startup-tp_ujupo/live-result.json) |
| SQLite 与 SeaweedFS | [结果](../../tmp/live-cli-startup-q9hryqxe/tree-delta-browser.json) | [回执](../../tmp/live-cli-startup-q9hryqxe/live-result.json) |
| PostgreSQL 与 SeaweedFS | [结果](../../tmp/live-cli-startup-x0w682gr/tree-delta-browser.json) · [切换算例](../../tmp/live-cli-startup-x0w682gr/tree-after-switch.png) | [回执](../../tmp/live-cli-startup-x0w682gr/live-result.json) |

四组都使用生产前端和两个实际 API。对另一进程创建、修改和删除的中文及 emoji 路径，已有文件夹持续展开，测试期间整树请求数为零。两种文件系统组合增加 257 个实际文件，浏览器通过整树恢复显示超出事件预算的末尾文件。网络断开后，另一进程的写入在重连后可见，握手携带前次已应用的游标。增量请求故障、服务端拒绝过大游标和切换后的迟到响应均通过。[外部监听兼容与 Windows SMB](FF-H09-external-file-watch-acceptance.md)

补验在 PostgreSQL 部署中，从 Windows SMB 回环路径独立写入文件，浏览器只请求新增路径，现有目录保持展开。该轮也通过断线恢复，浏览器使用已应用的游标 26 重连，记录 2 次真实模型 HTTP 200。使用现有本机 SMB 路径的 Windows 环境可设置 `VIBEFLOW_TEST_TREE_NETWORK=1` 复跑。[SMB 浏览器记录](../../tmp/live-cli-startup-y3fekuys/tree-delta-browser.json) · [SMB 模型回执](../../tmp/live-cli-startup-y3fekuys/live-result.json)

四组最终专项共 8 次真实 `deepseek-flash` HTTP 200。当前前端另在 PostgreSQL 与 SeaweedFS 上通过工作区搜索、打开命中文件和只读组织权限回归，另有 2 次 HTTP 200。模拟调用为 0。前端生产构建通过，Impeccable 检测结果为空。[工作区搜索](../../tmp/live-cli-startup-l503giw8/workspace-search.json) · [界面检测](../../tmp/a12-tree-detector-20261119.json)

早期测试的 WebSocket 代理在若干故障注入轮次超时，已改用真实浏览器连接和离线控制。测试在发起下一处故障前核对前一次事件游标，避免前一个仍在进行的整树恢复提前涵盖后一个样本。[早期 S3 超时](../../tmp/live-cli-startup-41j9bjey/live-result.json) · [游标阶段记录](../../tmp/live-cli-startup-oce8q4bd/tree-delta-browser.json)
