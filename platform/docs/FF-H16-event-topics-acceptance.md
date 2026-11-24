# FF-H16 实时事件的账户与算例范围

旧版 `/ws/events` 将临时事件一律当作算例事件。隔离作业请求确实因为并发额度返回 429，打开的两个合法连接均收不到 `quota.warning`。`runtime.degraded` 同样由账户发布，载荷没有算例 ID。[修复前 WebSocket 记录](../../tmp/live-cli-startup-sic8zrqr/event-topics.json) · [缺失事件断言](../../tmp/live-cli-startup-sic8zrqr/live-result.json)

现在只允许 `quota.warning` 和 `runtime.degraded` 这两类无算例 ID 的账户通知到达同一账户的合法连接。`scene.available` 仍按算例发送，`files.changed` 和 `jobs.updated` 的持久事件仍由数据库按算例顺序回放。临时事件发送前重新核对账户和算例访问权，订阅在 `ready` 帧发送前建立。断开连接会回收订阅和监听任务。

实际 S3 初轮还发现，可视化会话暂时独占算例存储时，文件快照抛出 `CaseWriteConflict`，异常处理试图在已建立的 WebSocket 上发送 HTTP 响应。现在文件监听保留上次有效快照，遇到算例写入排斥和临时资源冲突时等下一轮。授权失败仍关闭连接。[S3 断开日志](../../tmp/live-cli-startup-j3tqbbfg/api.log) · [断开前已收到的事件](../../tmp/live-cli-startup-j3tqbbfg/event-topics.json)

## 四种数据库与存储组合

| 组合 | WebSocket 与操作证据 | 真实 DeepSeek 工具回读 |
|---|---|---|
| SQLite 与文件系统 | [实测](../../tmp/live-cli-startup-b257zy4m/event-topics.json) | [模型回执](../../tmp/live-cli-startup-b257zy4m/live-result.json) |
| PostgreSQL 与文件系统 | [实测](../../tmp/live-cli-startup-0qbmq0tl/event-topics.json) | [模型回执](../../tmp/live-cli-startup-0qbmq0tl/live-result.json) |
| SQLite 与 S3 | [实测](../../tmp/live-cli-startup-z_qoxe44/event-topics.json) | [模型回执](../../tmp/live-cli-startup-z_qoxe44/live-result.json) |
| PostgreSQL 与 S3 | [实测](../../tmp/live-cli-startup-hr9owokv/event-topics.json) | [模型回执](../../tmp/live-cli-startup-hr9owokv/live-result.json) |

每组实际启动两个独立 API 进程。另一实例写入文件并执行 Windows 作业，订阅端收到原文件事件及运行、成功事件，重连后从已知序号读回相同 UUID。实际额度拒绝产生 429 和 `quota.warning`。隔离服务请求一个实际不存在的 WSL 发行版，运行时真实报告不可达，两条同账户连接收到 `runtime.degraded`。可视化服务实际打开 STL、推送创建与关闭并核验进程清理。

同一账户的其他算例只收到上述账户通知，其他用户完全隔离，直接进入别人的算例连接返回 403。文件存储移除算例后连接返回 4403，S3 撤回真实工作区共享后也返回 4403。连接全部关闭后订阅数为零。另用固定的资源冲突和节流输入核对略过快照时仍能计算正确的后续文件差异，该检查没有模型环节。[快照协议结果](../../tmp/FF-H16-snapshot-deferral-20261119.json)

Chromium 在两种存储上复验断网时的实际作业失败、联网补拉、重复和乱序提示、刷新后保留原作业。其中一个初轮在客户端尚未采用已返回的游标时就断网，导致验收等待超时。复跑改为等待后续 HTTP 请求明确使用非零游标，再断网。四组事件和两组浏览器最终共有 12 次真实 `deepseek-flash` HTTP 200，本阶段含初轮通过组共 18 次 HTTP 200，模拟调用为 0。[文件系统浏览器](../../tmp/live-cli-startup-6ab9185r/browser-replay.json) · [S3 浏览器](../../tmp/live-cli-startup-cs5fwy7j/browser-replay.json) · [浏览器初轮记录](../../tmp/live-cli-startup-6h4ckfir/live-result.json)

使用服务器虚拟环境运行 `live_llm_event_topics_acceptance.py --source-profile-id 26`，可追加 `--postgres` 与 `--s3`。变更可随应用版本回滚，无数据库迁移。持久事件已经跨 API 实测，跨实例转发临时事件仍归 FF-J14。[验收入口](../server/live_llm_event_topics_acceptance.py)
