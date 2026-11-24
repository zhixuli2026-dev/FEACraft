# S3 沙箱目录发布与恢复

## 2026-11-21 复用本地快照发布

目录确认恢复和沙箱正常关闭在安装文件时直接读取已验证的本地快照。S3 日志仍写入并核对前后副本。新的物理对象先核对 SHA-256、长度和对象元数据，再通过条件更新进入公开目录。仅凭远端日志进行的恢复继续按原额度下载暂存。 [目录事务](../server/app/services/s3_file_transactions.py) · [发布前正文核验](../server/app/services/s3_live_store.py)

认证 SeaweedFS 上，真实 WSL 40 MiB 确认恢复和正常关闭的正文安装阶段新增本地暂存从 40 MiB 降至 0，另案保存仍返回 200，同案并行写入返回 409。正常关闭期间实际续租；期限耗尽后等待传输结束，再保留原文件和字典修订。 [旧版 40 MiB 暂存](../../tmp/s3-reserved-tree-catalog-20261121.json) · [正常关闭与超时](../../tmp/s3-tree-retained-close-20261121.json) · [确认恢复](../../tmp/s3-tree-retained-restore-20261121.json)

独立验收把 12 MiB 本地文件在实际上传开始时改成同长度内容，SeaweedFS 收到错误字节，公开对象版本和内容仍是原件。改动 S3 日志也会拒绝提交。移开本地源后可从日志回滚，独立使用日志重新安装时观测到 12 MiB 本地暂存。 [错误正文与日志回滚](../../tmp/s3-retained-tree-source-20261121.json)

API 在实际安装 40 MiB 正文后、写入 SQL 见证前以退出码 87 终止。重启后直接从 S3 读到已安装的新正文。临时移开本地快照，正式恢复接口只凭远端日志回滚到原文件。放回快照后按原请求标识重试并读回完整摘要，重复确认复用同一回执。模型和模拟调用均为 0。 [API 中断、回滚及重试](../../tmp/s3-tree-retained-cold-final-20261121.json)

普通文件兼容回归通过 40 MiB 恢复、超时返回 504、同标识重试、字典和两条历史的失败回滚。该入口的正文安装暂存仍为 40 MiB。 [普通文件与字典兼容](../../tmp/s3-tree-retained-ordinary-compat-20261121.json)

## 2026-11-21 批量读取目录索引

已有目录索引的算例先锁定一次索引版本，再按实际物理对象的 ETag 读取正文。结束时重取目录索引比对。在应用确认计划前，逐个 HEAD 校验文件版本、字节数和权限描述对象。物理对象同长度改写和公开路径版本变化均拒绝旧计划，内部作业日志变化仍可继续。历史目录的读取兼容已复验。 [实现](../server/app/services/tree_transactions_s3.py)

认证 SeaweedFS 对同一批 258 个普通文件实测，包含一个 12 MiB 文件、空文件和目录权限描述。新旧两条读取路径得到完全一致的树、摘要和模式位。 [原始请求及字节统计](../../tmp/s3-tree-catalog-20261121.json)

| 指标 | 旧读取路径 | 固定索引版本 |
| --- | ---: | ---: |
| 完整索引 GET 次数 | 521 | 2 |
| 索引响应字节数 | 44,754,942 | 171,804 |
| 对象正文响应字节数 | 12,585,698 | 12,585,698 |
| 本轮扫描用时 | 6.75 秒 | 1.36 秒 |

通过认证 SeaweedFS 的实际 WSL 40 MiB 正常关闭、源版本变化拒绝和快照确认恢复。恢复传输期间另案保存返回 200，同案竞争返回 409。恢复保留空目录和权限，重复确认复用同一回执。公开预览读正文期间同案保存返回 200，过期预览返回 409。真实模型和模拟均为 0。 [真实关闭](../../tmp/sandbox-normal-close-resource-87zq8e0t/sandbox-normal-close-resource.json) · [确认恢复](../../tmp/s3-reserved-tree-catalog-20261121.json) · [公开预览](../../tmp/sandbox-recovery-ui-retirement-_oyevsd_/local-tree-proof.json)

本地 ext4 与 tmpfs 的跨卷目录恢复、导出及两次真实 API 中断复验通过。 [本地目录兼容](../../tmp/s3-tree-catalog-local-compat-20261121.json)

2026-11-20。正常关闭沙箱时的目录发布，以及从已验证快照执行的目录确认恢复，都复用既有 S3 存储操作预约。算例预约先落库，源文件和目标版本校验、前后副本传输、活跃对象写入与恢复排空都在事务外进行。最终短事务同时提交文件见证与业务回执。正常关闭涉及 caseDict 时，同一事务提交它的修订号。[存储预约](../server/app/services/storage_operations.py) · [提交实现](../server/app/services/s3_staging_transactions.py)

大文件操作共用现有四个复制名额，满载立即返回 429。每次物理 S3 写入经原预算登记，下载暂存沿用节点空间预留。共享的单次操作期限覆盖预约、网络传输和提交。取消或到期先等持有的工作线程结束，再按是否存在 SQL 文件见证决定回滚或结清。活动预约期间，同一算例的竞争写入返回 409，其他算例可正常保存。[目录事务实现](../server/app/services/tree_transactions_s3.py)

## 实际验收

| 场景 | 实测结果 |
| --- | --- |
| 正常关闭 | 真实 WSL 生成 40 MiB 文件，认证 SeaweedFS 生产路径在首个 1 MiB 对象块暂停读取。节点暂存预留为 40 MiB，SQLite 可立即取得独立写事务，同账户另案保存 HTTP 200，同案竞争 HTTP 409。暂停期间终端实际续租。结束后核对完整摘要、空目录、caseDict 新修订及单一导出见证。 |
| 期限到达 | 实际 40 MiB 流在单次 24 秒测试期限到达时仍未排空。释放后完成回滚，原文件字节和 caseDict 修订保留，发布事件为空，存储预约和物理写入额度结清。首轮 9 秒夹具在进入目标正文前已到期，最终以记录到首个真实对象块的复验作为证据。 |
| 已验证快照恢复 | 实际退出原 API，核验进程停止并恢复 WSL 沙箱快照。目录确认恢复在实际 40 MiB 对象传输期间允许另案保存，同案返回 409。最终核对摘要、空目录、权限恢复回执和同一请求的幂等重放。 |
| 原正常关闭回归 | 保留的 40 MiB 远端源仅完整读取一次，读取时 SQLite 可以写入。外部树版本变化返回冲突并保留私有导出，随后正常保存通过。 |
| 本地文件系统回归 | 同一 WSL 正常关闭仍发布空目录、文件和 caseDict 修订，40 MiB 文件摘要一致，原生进程退出已核验。 |

本轮 S3 验收使用非特权 WSL、SQLite 和认证 SeaweedFS。真实模型 0，模拟模型 0。[正常关闭首轮的 `normal` 记录](../../tmp/sandbox-reserved-export-20261120.json) · [超时排空与回滚](../../tmp/sandbox-reserved-timeout-20261120.json) · [真实快照恢复](../../tmp/sandbox-reserved-recovery-20261120.json) · [原正常关闭回归](../../tmp/sandbox-normal-close-resource-8talhwds/sandbox-normal-close-resource.json) · [本地文件系统回归](../../tmp/sandbox-recovery-ui-tree-export-9_xk9wod/local-tree-proof.json) · [实测入口](../server/sandbox_reserved_tree_acceptance.py)
