# FF-H09A 写入提交后的即时索引验收

修复前，暂停后台事件投递后，caseDict 已提交，直接查询索引仍没有新内容。发起搜索会触发当前存储对账，所以仅检查搜索结果会掩盖这个窗口。[旧版反例](../../tmp/live-cli-startup-4f4ezg0o/immediate-file-index.json)

文件事务收尾只读取已持久化的 `files.changed` 事件。已有索引时，用其路径提示核对当前文件版本，并在单个索引事务内增量更新受影响的路径。普通上传、分块上传发布、caseDict 保存和 Agent 写入共用同一收尾路径。无关的全文行和行号保留。未启用的索引仍由用户创建。路径提示更新后不再沿用原全库指纹，后台事件和搜索对账仍负责完整性核验。

快速更新共用现有文件读取线程许可，时间预算最多三秒。索引锁忙、文件读取失败或旧索引缺少当前行号映射时，已提交的业务写入保持有效。持久投递和事件消费回执负责后续补齐。没有业务数据库迁移，应用可以按原版本回滚。[持久投递机制](durable-index-consumers.md)

## 实测矩阵

| 数据库与存储 | 写入和索引记录 | 真实 DeepSeek 回执 |
|---|---|---|
| SQLite 与文件系统 | [实测](../../tmp/live-cli-startup-mbs03ij8/immediate-file-index.json) | [模型](../../tmp/live-cli-startup-mbs03ij8/live-result.json) |
| PostgreSQL 与文件系统 | [实测](../../tmp/live-cli-startup-7guweh25/immediate-file-index.json) | [模型](../../tmp/live-cli-startup-7guweh25/live-result.json) |
| SQLite 与 S3 | [实测](../../tmp/live-cli-startup-8rh4e7jt/immediate-file-index.json) | [模型](../../tmp/live-cli-startup-8rh4e7jt/live-result.json) |
| PostgreSQL 与 S3 | [实测](../../tmp/live-cli-startup-h532wqf3/immediate-file-index.json) | [模型](../../tmp/live-cli-startup-h532wqf3/live-result.json) |

四组都在暂停后台投递、未连接 WebSocket 的环境中操作。caseDict 保存、普通上传、分块发布、Agent 批准写入和 Agent 自动写入均在第一次搜索前直接从 SQLite FTS 得到精确命中。分块发布前和 Agent 批准前保留原索引；批准替换后旧内容无命中，另一个文件的行号和摘要一致。其他账户的审批和搜索返回 404。

在另一连接取得真实 SQLite 索引写锁时，测试再次上传。业务接口正常返回，原始文件可读，此时索引没有这条内容。实际重启 API 后，之前已索引的内容仍可用。解除投递暂停后，缺失行由原事件补齐并留下唯一回执，四组均通过公开搜索读取全部结果。最终四轮每轮 6 次 `deepseek-flash` HTTP 200，包含实际提案、批准后续回合、自动工具写入和独立摘要回读，模拟调用为 0。

使用服务器虚拟环境运行 `live_llm_immediate_file_index_acceptance.py --source-profile-id 26`，可追加 `--postgres` 和 `--s3`。跨主机独立索引副本同步仍由 FF-J14 跟踪。[验收入口](../server/live_llm_immediate_file_index_acceptance.py)

## 既有搜索回归

SQLite 与文件系统、PostgreSQL 与 SeaweedFS 的既有搜索回归均通过。并发替换只有一个成功，异账户读取和改写均返回 404，私有作业输出保持不可见，已发布的作业产物在首次搜索前进入 FTS。两轮各有 2 次真实 `deepseek-flash` HTTP 200，模拟调用为 0。[文件系统结果](../../tmp/live-cli-startup-ewx4dvnk/search-updates.json) · [文件系统模型回执](../../tmp/live-cli-startup-ewx4dvnk/live-result.json) · [S3 结果](../../tmp/live-cli-startup-zkkjnk7a/search-updates.json) · [S3 模型回执](../../tmp/live-cli-startup-zkkjnk7a/live-result.json)

S3 旧用例要求同一秒完成两次写入和中间的一次搜索，三轮补跑都未得到同秒样本。复验先在真实对象存储中写入两个 39 字节的不可变对象，核对其 LastModified 相同，再通过正常的清单条件更新依次发布。每次发布后都发起真实 HTTP 搜索。第二个版本的 ETag 不同，替换后旧正文无命中。[最后一次旧流程失败](../../tmp/live-cli-startup-il50afo5/live-result.json) · [同秒对象与新正文命中](../../tmp/live-cli-startup-zkkjnk7a/same-second.json)
