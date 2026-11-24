# API 启动时的会话与工具恢复

2026-11-18，修复了 API 重启时的两个问题。原查询每次只检查编号最小的 64 个执行归属。前面的会话持续活跃时，较晚退出的执行者可能一直得不到恢复。原工具清理先列出受保护的会话，再按该名单批量更新；在两步之间接手工具的进程可能被误标为中断。

## 启动行为

每次扫描先读取当时最大的候选会话 ID，随后按升序每页最多读取 64 个。每页从上一页最后一个 ID 之后继续。已退出的 API、审批和目标回合仍须通过原有进程身份核验。恢复事务同时提交工具状态、回合与请求终态、唯一回执和归属释放。启动汇总只保留计数和前 64 个错误样本，可按会话读取持久回执。[实现](../server/app/services/agent_recovery.py)

旧版无归属工具按算例加锁，更新时再次查询该会话是否已有执行者。旧版回合与请求按各自 ID 的最后一条状态挑选候选，终态会话可以跳过写锁。恢复工具历史时按页读取助手记录中的工具字段，按调用 ID 查实际结果；从数据库只取最后一条尚未被后续助手消息覆盖的断点。[启动入口](../server/app/main.py)

## 实际进程、数据库与模型

SQLite 配本地文件系统、PostgreSQL 配 S3 对象存储均走正常 API 启动入口。模型使用用户已授权的 DeepSeek 配置。下列三组涉及模型的行为均由真实上游产生。

- **故障恢复。** 先在真实 API 进程登记 65 个仍然有效的低编号归属。另一个 API 持久化真实 DeepSeek 输出的断点后被终止。新 API 分两页读取 67 个归属，页大小为 64 和 3。被终止的会话仅产生一份回执，模型续聊时找回原先自行生成的标识。另一条真实流在同伴启动前已提交断点，启动后正常完成，同一回合仅有一次开始和一次完成。[SQLite 进程验收](../../tmp/live-cli-startup-fxemhf_i/agent-checkpoint-recovery.json) · [PostgreSQL/S3 进程验收](../../tmp/live-cli-startup-g3fr4uhm/agent-checkpoint-recovery.json)
- **工具归属竞争与历史。** 测试在选中两个旧工具后，让另一个 API 完结其中一个旧工具并取得该会话归属，新工具保持运行，另一会话的孤立工具被标为中断。另置 67 个旧版会话的 SQL 生命周期记录，两页实际选中 64 和 2 个，共追加 33 个回合中断和 33 个请求中断；第 67 个会话原有的完成状态得到保留。该组规模数据仅含协议记录。[SQLite 候选页](../../tmp/live-cli-startup-he5jkqae/legacy-pages.jsonl) · [PostgreSQL/S3 候选页](../../tmp/live-cli-startup-v116fs_6/legacy-pages.jsonl)
- **审批兼容。** 两种配置各验证待用户批准、写入提交前、写入提交后三个实际进程退出位置。已提交的文件变更保持一次，待批准的提案仍需用户操作，重试旧批准不会重复写文件。工程流程在用户明确确认后才推进。[SQLite 审批](../../tmp/live-cli-startup-idirhagb/agent-approval-recovery.json) · [PostgreSQL/S3 审批](../../tmp/live-cli-startup-51tygja4/agent-approval-recovery.json)

本阶段共 10 轮。8 轮会话恢复含逐步增加的历史分页、活跃流正常完成和启动汇总检查，另 2 轮为审批兼容。实际 DeepSeek 聊天请求 54 次，均返回 HTTP 200，模拟调用 0 次。最终两轮各记录 5 次请求。两种数据库的最终启动汇总相同，检查 67 个归属，恢复 1 个，跳过 66 个，失败 0 个。隔离 API 均已退出，5 个临时 PostgreSQL 数据库均已移除，对应对象存储进程均已停止。[逐轮请求与清理记录](../../tmp/agent-startup-reconciliation-20261118.json) · [最终 SQLite](../../tmp/live-cli-startup-olmk_e_b/agent-checkpoint-recovery.json) · [最终 PostgreSQL/S3](../../tmp/live-cli-startup-a4r8qemj/agent-checkpoint-recovery.json)

主线 **50/53，剩 3 项**，全功能 **18/295，剩 277 项**，资源预算 **12/17，剩 5 项**。此次推进 N4-05 的 RB-15 和 FF-F42 的重启恢复。FF-F42 的检查点时间和字节阈值、浏览器长历史窗口中恢复半截正文与推理的完整验收仍待完成。大量同时未结清的工具仍须逐一对账并写入回执，本次未测该情况下的最大内存。
