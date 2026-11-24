# RB-12 部分验收：远端 staging 逐笔写入预留

## 2026-11-21 终端和旧作业复用工作副本

已通过停止核验的普通终端，以及旧作业与原生 CLI 的 S3 暂存同步，共用保留本地源的发布路径。先绑定算例、暂存目录和文件摘要，验证独立的 S3 日志，再读取本地工作副本。新对象经 SHA-256、长度和元数据核验后，通过条件更新进入公开目录。仅凭日志回滚时按实际下载申请空间。

Windows `cmd` 和非特权 WSL 的两次实际终端退出各回写 1,310,720 B。两次均验证停止范围，读取原工作副本，安装阶段新增本地暂存均为 0。另用实际远端写入制造冲突，两边文件和暂存清单均可读回；冷启动保留原终端停止回执。 [终端验收](../../tmp/s3-terminal-workspace-budget-x71sqcq5/terminal-s3-workspace-budget.json)

独立认证 SeaweedFS 验收使用 12 MiB 工作副本。上传过程中改动同长度正文时，错误内容不进入公开目录；改动日志拒绝应用，移开本地文件后仍可回滚。另以两个真实 API 验证旧作业正常提交和首个文件应用后的实际退出，冷恢复保留原文件。 [12 MiB 传输与故障](../../tmp/s3-staging-retained-source-20261121.json) · [旧作业回滚](../../tmp/staging-transfer-evidence-yma0rq1q/checks.json)

SQLite 与认证 SeaweedFS 上的原生 WSL CLI 通过八项恢复检查。两个真实 `deepseek-flash` 回合分别进入审批和执行恢复文件的读取，第二回合持久化了精确回复。实际 API 退出、远端冲突、清理失败和删除完成后再次退出均有回执。模拟调用为 0，CLI 内部 HTTP 次数未采集。PostgreSQL 首轮在准备测试文件时遇到存储盘点 503，进入模型回合前结束，临时库已删除。 [SQLite 实测](../../tmp/live-cli-staging-recovery-a0z_46ya/live-result.json) · [PostgreSQL 首轮](../../tmp/live-cli-staging-recovery-sqyp3k_9/live-result.json) · [临时库清理](../../tmp/cli-staging-recovery-postgres-_0bmhqeb/result.json)

## 2026-11-19 算例 S3 预留对账轮转

每个已取得持久算例锁的存储操作，按最近进入对账的时间与 ID 选择最多 16 条未结清记录。候选范围绑定当前算例、账户和对象存储。选中页在短事务内记录核验顺序，退出该事务后再读取对象。未能核验的条目保留原预留和回执，后续页面可以继续推进。获得精确长度、摘要与远端版本证据时，才通过已有结算事务释放预留。[逐笔写入与结算](../server/app/services/remote_write_budget.py) · [算例操作锁](../server/app/services/storage_operations.py)

两种数据库均连接实际启用版本的 SeaweedFS。测试页缩为 2 条，种下三条旧预留，并通过真实逐笔预留发送一个 39 字节的 S3 PUT。对象存储接受写入后，测试入口丢弃响应，产生一笔真实待结算的请求。第一页处理旧记录，第二个 API 进程重启后接续核验后面的原对象。SHA-256、ETag 和物理对象版本匹配，结算完成后远端版本不变。三条不明、损坏或摘要不符的预留合计 167 字节，保留原额。外账户请求被拒绝。同一算例的第二次恢复返回 409，同一账户的另一个算例可在对象读取暂停时正常提交文件。专项两轮各有 2 次真实 `deepseek-flash` HTTP 200，模拟调用为 0。[SQLite 证据](../../tmp/live-cli-startup-88y40ztx/case-reservation-rotation.json) · [PostgreSQL 证据](../../tmp/live-cli-startup-69slyra5/case-reservation-rotation.json) · [复跑入口](../server/live_llm_case_reservation_rotation_acceptance.py)

PostgreSQL 的正式 WSL CLI 暂存恢复回归通过 7 项。实际退出 API 后恢复文件，拒绝变化后的存储位置，保留远端冲突文件，部分清理失败后重启接续，最终只释放一次执行归属。两个真实 DeepSeek CLI 回合中，后续回合读取恢复后的远端文件并持久化精确回复，CLI 内部 HTTP 次数未采集。首次运行的隔离环境缺少 WSL CLI 路径，启动即返回 503。确认现有 Linux 程序可由非特权账户运行后，使用进程环境指定程序与独立私有测试目录重跑。[正式回归](../../tmp/live-cli-staging-recovery-846c0c30/live-result.json) · [首次失败记录](../../tmp/live-cli-staging-recovery-ci3p4o_x/live-result.json)

本批无数据库迁移，旧代码读取相同回执与预留。未知可变写入的人工处置、旧记录压缩及私有缓存历史继续由 RB-15 跟踪。[资源工作清单](resource-budget-worklist.md)

后续更新：S3 创建、克隆与恢复的账户持久预留已在[后续阶段](owner-storage-admission.md)实现并验证。下文保留本阶段边界；当前剩余工作以资源工作清单及主线台账为准。

2026-11-06：为持久存储操作内的每一次物理 S3 PUT 增加用户额度预留，覆盖 staging 同步与其恢复。RB-12 仍未整项完成；主线 33/53 完成、剩 20 项，资源预算阶段 11/17 完成、剩 6 块。

## 实现与边界

`storage_operations.hold` 建立额度上下文。`s3_live_store` 的底层客户端在发送 PUT 前，将 `StorageWriteReservation` 提交到独立事务，绑定用户、算例、存储位置、持久操作、写入进程、对象键、条件、长度与摘要。用户已有活动预留参与所有存储准入。因此正文、before/after 副本、manifest、HEAD 索引和恢复记录分别按实际编码长度计费，不再只按业务正文估算。

每笔预留对应一次 SDK 请求，关闭 SDK 自动重试；逻辑层的条件竞争重试会重新准入。收到明确成功响应或 412 拒绝后结算，已写入对象及保留版本由物理用量盘点计费。网络结果不明、进程退出和业务 SQL 回滚都保留全部未确认额度。上传流以准入长度限制读取和 Content-Length，源流变大不会扩大该请求的分配量。

原先进入后续存储操作时，按创建时间读取最多 100 条活动记录。现已由上面的轮转逻辑替代。可变 HEAD、能力探测对象、缺失或损坏对象继续保留额度。

PUT 在准入事务提交之后执行，业务提交与预留结算分离。现有配额盘点仍在用户事务锁内读取远端用量，逐笔写入还会重复盘点；本次没有解决慢存储导致的锁持有时间和扫描成本，继续由 RB-14、RB-15 跟踪。当前会保守地同时计入已写入但尚未结算的物理字节和预留字节，不能把它解释为精确可用空间。

该上下文覆盖 `staging_sync.sync_checked` 和 `s3_storage_recovery.recover`。普通文件事务、算例创建与克隆尚未接入；这些入口持有调用者 SQL 事务，创建的算例甚至尚未提交，不能直接复用独立写事务而造成 SQLite 死锁，也不能提前提交业务事务来绕过此问题。每笔准入保证当前写入额度，不承诺整个多文件操作一定完成；中途拒绝依旧依赖原有恢复协议。

## 验收与迁移

`remote_write_budget_smoke.py` 使用隔离 API、真实支持版本的 S3 对象存储及 SQLite/PostgreSQL。每库 13 个场景覆盖正常写入、逻辑对象与 HEAD 的实际版本字节、配额拒绝、未知结果、完整对象核验、进程退出、损坏对象、SQL 回滚、412 条件失败、源流增长，以及无条件写入和 SQL 控制事务内重入拒绝。

迁移 `c0a5c71846ed` 新增预留表及约束，降级遇到任何保留记录都会拒绝，防止丢失计费证据。隔离双库检查表结构、非法状态、外键与降级；开发数据库没有执行此迁移。

本阶段的兼容场景与最终源码绑定见 [验收记录](../../tmp/mainline-remote-write-budget-evidence/validation.json) 和 [源码绑定](../../tmp/mainline-remote-write-budget-evidence/source-binding.json)。完整剩余范围继续记录在 [资源工作清单](resource-budget-worklist.md)。
