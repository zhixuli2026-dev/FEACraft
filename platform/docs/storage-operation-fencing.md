# 文件事务清单与补偿恢复的数据库占用

2026-11-05，N2-06 第十八批。五类 staging 同步已使用数据库存储操作记录，在不长期持有算例 SQL 锁的情况下发布/读取文件事务清单、传输内容并执行补偿。新增显式文件事务恢复 API。整个 N2-06 仍未关闭。

## 已复现的问题

第十七批已移走批量传输，但 `publish_locked` 仍在算例 SQL 事务内执行 S3 清单请求。把暂停注入实际 pending 发布后，心跳无法续约，10 秒测试租约过期。证据为 `tmp/staging-metadata-lock-before.json`；这是修复前的一次性复现，不应在修复后重跑充当基线。

## 新的占用协议

新增 `case_storage_operations` 保存算例、来源、操作目的、原生进程身份、文件日志绑定、状态和结束时间。部分唯一索引保证每个算例最多一个 `active` 操作。状态包括 `active`、`drained`、`exited`、`settled`，没有通过过期时间抢占写入权的分支。

取得占用时先授权和读取候选记录，释放 SQL 事务后核验原进程，再重新取得控制锁核对候选没有变化。活跃、未知主机或无法核验的进程不能被替换。只有确认原进程已退出才记录 `exited` 并取得新占用。已有来源校验预约仍阻止存储操作。

普通算例协调入口和内容写入准入会检查占用；其他执行上下文收到 `file_sync_busy`，尚未核对的已排空记录收到 `file_recovery_required`。当前占用者以精确的操作 ID、进程身份和来源绑定进入短控制事务，可以跳过旧的事务内远端日志读取。心跳和取消仍能取得算例控制锁。

同步流程在此占用下先恢复旧日志，再收集、准备、绑定日志、发布 pending、条件写入，最后短事务写入 SQL 提交证明。所有相关线程和提交/回滚结束后，会核对提交证明并立即清理 pending 或执行补偿。失败、取消和未知提交不会绕过此核对。开始、核验退出、恢复及结束均产生持久事件。

恢复自身也取得同一种数据库占用，整批备份校验和补偿 I/O 在 SQL 事务外执行。另一个恢复者不会同时运行；恢复过程中再次退出后，新 API 可以重新核验并继续。无法核对的外部内容、清单或归属保留现场。

SQL 操作记录绑定了日志 ID、计划摘要和事件 ID，因此匹配的已排空/退出记录可作为额外排空证明。远端可选 `drained.json` 损坏时，如果存在精确匹配的 SQL 证明，本批允许继续核对恢复；不存在匹配证明的旧日志仍按原保守规则处理。此变化没有把内容或元数据冲突当作可覆盖状态。

## API 与迁移

- `GET /api/v1/cases/{case_id}/file-transactions`：存在数据库占用时直接返回占用/待恢复状态及操作 ID，不等待远端存储。没有新操作记录的旧日志仍沿用原诊断路径。
- `POST /api/v1/cases/{case_id}/file-transactions:recover`：重新检查活跃用户和算例写权限，取得独立占用后恢复；S3 返回本次恢复结果，FS 保留原恢复行为。共享只读权限不授予恢复写入权。
- 新迁移：`7b50d2c6f198`，前一版本 `6a4fc1b5e087`。部署本批代码前需执行经过审核的 Alembic 升级。存储操作表非空时拒绝降级，避免丢弃排空/归属历史。

本次只迁移了独立测试数据库，没有迁移开发数据库、重载端口 8000 或自动部署。恢复界面的操作入口与完整审计展示仍属于原后续范围。

## 验收

| 范围 | 结果 | 原始证据 |
| --- | --- | --- |
| pending 发布延迟：正常、用户取消、任务取消 | 双库各 3 项通过 | `tmp/job-finalizing-io-evidence-dyq3pg5b/result.json` |
| 文件事务清单读取延迟：同上 | 双库各 3 项通过 | `tmp/job-finalizing-io-evidence-nge16dzm/result.json` |
| 补偿回滚延迟：同上 | 双库各 3 项通过 | `tmp/job-finalizing-io-evidence-9pll5ad3/result.json` |
| 真实 S3 正常旧作业 | 1 组通过 | `tmp/staging-transfer-evidence-x7qur1r5/checks.json` |
| 真实 S3 活跃保护、取消、部分写入退出、SQL 提交前/后退出、响应丢失、竞争、上笔 pending | SQLite 8 组通过 | `tmp/staging-transfer-evidence-lcqh2dck/checks.json` |
| 清单发布前退出、清单响应丢失、补偿再次退出、未知主机、损坏远端排空记录且 SQL 证明有效 | SQLite/PostgreSQL 各 5 组通过 | `tmp/staging-transfer-evidence-azn8qpp1/checks.json`、`tmp/staging-transfer-evidence-ev1rjfn2/checks.json` |
| 新操作表唯一占用、状态约束、历史降级保护、空表降级再升级和 schema 一致性 | 双库各 7 项通过 | `tmp/storage-operation-migration-1brg146z/checks.json` |
| 原完整数据库迁移/约束/历史保护 | 双库套件通过 | `tmp/workflow-migration-jahl1_na/result.json` |
| 四类交互会话正式传输延迟与心跳/控制响应 | 双库 8 组通过 | `tmp/interactive-staging-evidence-o5z60q74/checks.json` |
| 原普通文件事务正常、部分写入、删除、元数据兼容 | 4 组通过 | `tmp/s3-file-transactions-evidence-4bkpfq4n/checks.json` |

按上表计 65 项/组，其中原完整迁移按两套验收计数，没有把全部历史约束再累计为新增功能。所有临时 PostgreSQL 库已删除。PostgreSQL 新故障夹具首轮缺少新恢复进程的身份查询接口，已补齐并整组重跑通过；失败日志保留在 `tmp/N2-storage-operation-new-windows.log`。

延迟检查使用真实进程、独立数据库和文件系统 S3 替身；真实对象存储故障检查使用 SeaweedFS。进程退出测试覆盖明确暂停边界，不代表已经覆盖服务端仍未完成的在途请求。

## 仍需推进

1. **产物别名清单路径**：`coordinate` 最后调用 `artifact_publication.invalidate_aliases`，其中 `managed_paths` 会在事务内读取已有产物清单。需要在持久占用下预读取并核对版本，避免此路径继续阻塞心跳。
2. 普通文件保存、没有新操作记录的旧日志自动恢复/诊断、启动克隆仍有事务内存储访问。本批的 staging 迁移不能替代这些入口的验收。
3. **在途远端请求**：进程退出与远端请求排空不是同一事实。已发出但服务端尚未完成的请求、晚到写入与恢复竞争仍需专门复现和处理；本批的暂停/退出检查不能证明该边界已安全。
4. SQL 最终排空记录写入失败、未知主机/PID 身份等情形仍保守阻止抢占；完整跨主机停止证明和操作界面继续。
5. 准备残留、未完成上传/输出、已提交缓存重建、索引对账、原 UDF 409、完整部署故障矩阵和原审计范围均保留。

主线仍为 23/53 完成、11 进行中、19 待开始，剩余 30；295 项严格整项交付仍为 0。
