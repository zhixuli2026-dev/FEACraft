# FF-H09 外部文件变更监听验收

## 2026-11-20 变更扫描、发布与写锁

后台监听在文件列表和路径差异计算前释放数据库连接。新观测发布时核对算例来源、兼容事务、事件水位、存储标记和上一份已提交观测，再同事务提交快照与 `files.changed`。旧结果发生竞争时等下一轮获取。旧文件事务的实际回滚经独立恢复入口接续，并通过无新变更事件的回归。[监听实现](../server/app/services/case_file_watch.py) · [事务恢复](../server/app/services/case_journal_recovery.py)

对照中外部文件发生变化后，Windows 文件系统的监听暂停在首条目录记录，独立 SQLite 写者收到 `SQLITE_BUSY`。修复后的文件系统和认证 SeaweedFS 均在实际枚举暂停时可取得独立 SQLite 写事务，另案保存 200。两套 API 的旧观测暂停，新观测先落库，两端收到同一个事件 ID。旧观测恢复后保留新版本，后续轮询没有新增重复事件，关闭最后一个连接后监听退出。[锁内基线](../../tmp/case-file-watch-writer-wfqx40zw/case-file-watch-writer.json) · [文件系统复验](../../tmp/case-file-watch-writer-bc4v_a5o/case-file-watch-writer.json) · [认证 S3 复验](../../tmp/case-file-watch-writer-_4x456b2/case-file-watch-writer.json)

认证 S3 的高频复验曾在空闲轮询主动更新目录清单时，外部条件写入多次遇到 412。已有清单的空闲监听现在只读。发布变更时先取得账户额度，以原扫描的 ETag 条件更新清单，旧命名空间保留原初始化方式。实际复验观察到空闲扫描前后 ETag 一致，旧 ETag 的更新被拒绝，最新清单保持。更早一轮专项在 S3 对象落盘前取了候选，观察夹具已改为等候包含目标路径的真实清单。[条件写入失败](../../tmp/case-file-watch-writer-ji5fcyi5/case-file-watch-writer.json) · [过期候选首轮](../../tmp/case-file-watch-writer-td0qvuay/case-file-watch-writer.json) · [清单复验](../../tmp/case-file-watch-writer-_4x456b2/case-file-watch-writer.json)

实跑 Windows 原生通知、PostgreSQL 加 SeaweedFS 和 PostgreSQL 加本机 SMB 回环。新增、同长度替换、删除、三连接事件一致、断开重连和索引更新均通过。SMB 轮实际 DeepSeek 选用 Agent 文件写入工具，两端收到同一监听事件。本轮合计 10 次 `deepseek-flash` HTTP 200，包含 SeaweedFS 调整前的 2 次及最终复验的 2 次。模拟 0 次。[Windows 模型回执](../../tmp/live-cli-startup-eyf1c0te/live-result.json) · [首轮 S3 模型回执](../../tmp/live-cli-startup-7aose9nt/live-result.json) · [SMB Agent 事件](../../tmp/live-cli-startup-27o1lvb1/external-file-watch.json) · [SMB 模型回执](../../tmp/live-cli-startup-27o1lvb1/live-result.json) · [最终 S3 模型回执](../../tmp/live-cli-startup-8yh4r12j/live-result.json)

原认证 S3 文件事务诊断、期限耗尽及冷恢复接续复验通过。实际 PostgreSQL 临时库及隔离对象服务完成清理。并发专项执行 `case_file_watch_writer_smoke.py`，加 `--s3` 使用认证 SeaweedFS。当前变更沿用原事件和目录格式，无迁移。[文件事务兼容](../../tmp/watcher-catalog-recovery-compat-20261120.json) · [临时 PostgreSQL 清理](../../tmp/job-repeat-postgres-njndhgiy/result.json) · [对象服务退出](../../tmp/s3-attempt-store-8mdj_cgi/job-repeat-run.json) · [专项代码](../server/case_file_watch_writer_smoke.py)

## 原验收范围

原监听给每条 WebSocket 单独维护内存快照，外部变更只发给当前连接。断线和暂时无法读存储都会丢失快照。文件比较只使用大小和秒级修改时间。

现在同一 API 进程内每个算例共享一个检测任务。文件系统由原生通知唤醒检查，并保留两秒轮询。S3 使用轮询，按 ETag 和对象元数据识别版本。文件系统比较纳秒修改时间、大小和目录名称。`VIBEFLOW_FILE_WATCH_NATIVE=false` 可让网络盘仅用轮询。最后一个连接断开时停止该进程的检测任务，数据库保留最近一次成功观测。下一次订阅会补查期间的净变更。[Watchfiles 的通知、轮询和停止接口](https://watchfiles.helpmanual.io/api/watch/) · [服务实现](../server/app/services/case_file_watch.py)

检测任务使用原算例协调器，将快照和 `files.changed` 同事务提交。两个 API 比较同一份持久快照，同一轮观测共享一个事件 ID。接收方沿用权限检查与事件游标，已有索引由原 outbox 消费更新。常规批次提供创建、修改和删除路径，首轮和超出消息预算的批次触发整树对账。原业务写入事件继续发布，检测暂时失败时保留上一份成功快照。[既有事件回放与消费](durable-index-consumers.md)

## 实际验收

| 部署与路径 | 检测记录 | DeepSeek 回执 |
|---|---|---|
| SQLite 与 Windows 文件系统，原生通知 | [验收](../../tmp/live-cli-startup-lv4y83dn/external-file-watch.json) · [原生事件](../../tmp/live-cli-startup-lv4y83dn/native-watch-events.jsonl) | [回执](../../tmp/live-cli-startup-lv4y83dn/live-result.json) |
| PostgreSQL 与文件系统，纯轮询 | [验收](../../tmp/live-cli-startup-vb75xh41/external-file-watch.json) | [回执](../../tmp/live-cli-startup-vb75xh41/live-result.json) |
| PostgreSQL 与文件系统，第二个 API 使用 Windows SMB 回环路径 | [验收](../../tmp/live-cli-startup-dl2x876g/external-file-watch.json) | [回执](../../tmp/live-cli-startup-dl2x876g/live-result.json) |
| SQLite 与 SeaweedFS | [验收](../../tmp/live-cli-startup-5e_esl2g/external-file-watch.json) | [回执](../../tmp/live-cli-startup-5e_esl2g/live-result.json) |
| PostgreSQL 与 SeaweedFS | [验收](../../tmp/live-cli-startup-ku3etoo3/external-file-watch.json) | [回执](../../tmp/live-cli-startup-ku3etoo3/live-result.json) |

五组均通过真实外部新增、同长度覆盖、删除、首次搜索前索引更新、三个连接的事件 ID 一致，以及断开全部连接后从另一 API 补拉。其他账户订阅被拒绝。外部写入使用独立进程或真实 S3 条件更新。SMB 组最后一次外部写入经过 UNC 路径，由轮询侧独立发现。同组的 DeepSeek 实际选择 `propose_file_update`，截取正文与预期字节一致，两端接收同一观察事件。原生组把首个 API 的轮询周期拉到 60 秒，在第二个 API 订阅前验证独立进程写入由实际 Windows 通知触发。

独立 Linux 容器运行相同监听代码，捕获另一个进程的写入，停止后监听任务退出。纯轮询故障试验暂时移走隔离算例目录，恢复后只报告新增文件。SQLite 与 PostgreSQL 均通过升级、降级、再升级并保留已有算例。[Linux 原生通知](../../tmp/file-watch-linux-20261119/result.json) · [目录恢复](../../tmp/file-watch-unavailable-dzmk5g1x/watch-recovery.json) · [迁移](../../tmp/file-watch-migration-013efb6c/result.json)

五组专项共 12 次真实 `deepseek-flash` HTTP 200。PostgreSQL 上两种存储的 H16 五类事件和权限回归另有 4 次 HTTP 200，模拟调用为 0。第一次 S3/SQLite 外部写入遇到清单 412，复验在保持原对象前置条件的情况下重试。第一次 SMB 误用模型配置路由，修正验收入口后通过。[H16 文件系统](../../tmp/live-cli-startup-e77uhfym/event-topics.json) · [H16 S3](../../tmp/live-cli-startup-6qr5b65j/event-topics.json) · [S3 首轮](../../tmp/live-cli-startup-51e04sr1/live-result.json) · [SMB 首轮](../../tmp/live-cli-startup-psgmeef9/live-result.json)

复验入口使用 `--source-profile-id 26`，按需追加 `--postgres`、`--s3`。有现成本机 SMB 路径的 Windows 环境可用 `--network-peer`。部署先应用 `228635dfe9a1`。回退时先停止新版 API，再降到 `e42a60d73fb1`，原事件和算例表仍可用。监视范围以成功快照为界，区间内发生又撤回的变更可能合并。跨物理主机部署矩阵继续由 N7-01、FF-J14 跟踪，浏览器增量树更新继续由 FF-A12 跟踪。[验收入口](../server/live_llm_external_file_watch_acceptance.py)
