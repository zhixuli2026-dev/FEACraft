# N7-02 PostgreSQL 迁移、回退与恢复

2026-11-12。按原任务的三项退出条件验收：旧 Job/日志不丢、备份可恢复、迁移前后 API 兼容性有结果。沿用本机 PostgreSQL 16 和已配置连接，仅创建随机命名的 `tmp_vf_n702_*` 临时库，不迁移开发数据库。

入口：`platform/server/postgres_migration_acceptance.py`。它首先执行 `regression_llm_policy.enforce`，源码与依赖哈希登记在 `platform/tools/regression-llm-review.json`。没有导入历史模拟测试，没有替换模型服务。五条旧 SQL 记录是迁移夹具，明确不作为实际执行或模型验收；新增 Windows shell 作业则通过生产 API 真正执行，并核对原生停止回执。

## 原条件与实际检查

| 原条件 | 验证方式 |
| --- | --- |
| 旧 Job/日志不丢 | 在初始修订 `6fb6c18fb7ee` 写入 succeeded、failed、cancelled、queued、running 五种历史记录，包含一条损坏的历史摘要。升级后、回退后、再次增量升级后逐字段摘要保持一致；五个 UTF-8 日志按实际字节校验。API 显示损坏摘要的诊断，保留旧记录；旧 queued/running 不被自动执行，不补造 attempt。 |
| 备份可恢复 | 真实 `pg_dump --format=custom` 备份升级前的 13 表和新旧混合的 52 表。分别通过 `pg_restore --exit-on-error` 恢复到另外两个临时库，逐表核对全部行内容摘要，逐文件核对 SHA-256，再启动生产 API 读回。混合备份包含实际新作业及原停止回执。 |
| 迁移前后 API 兼容性有结果 | 当前代码面对旧库明确拒绝启动并要求 Alembic 升级；没有静默改库。升级后登录、旧作业列表、详情、日志正常，实际新作业成功。恢复旧备份再升级后返回相同旧记录；恢复混合备份后，原新作业和日志也可读。未运行历史版本服务二进制，不宣称验证了任意版本的滚动兼容。 |

迁移路径为初始修订→head→初始修订→前一修订 `e8c3f09624ab`→head，再重复 upgrade head；`alembic check` 无 metadata 差异。仅有旧历史时回退后记录仍完整。加入实际新执行历史后，再向初始修订回退会触发 `Refusing to discard Job write-drain history`，整个 PostgreSQL 事务回滚，所有表的行摘要及修订保持原样。恢复采用原备份，没有删除执行历史来强行回退。

测试 API 使用真实独立进程；新 shell 作业停止核验完成后终止 API。进程终止退出码是 1，表示测试主动结束服务，不作为优雅关闭验收。四个 API 进程均退出，三个临时库均在不使用 `DROP ... FORCE` 的情况下删除并查询确认不存在。优雅关闭和跨主机矩阵仍属于 N4-07/N7-01。

## 证据与一次配置修正

首轮 `tmp/postgres-migration-aryc8wpp/result.json` 的数据库和 API 检查通过，但独立核对发现遗漏 `VIBEFLOW_AUDIT_DIR`：一次真实测试提交追加到了默认审计文件。因此首轮不能单独作为完整隔离验收，`isolation-review.json` 明确保留这个结论。

修正时匹配新增尾记录的时间、作业 ID、命令摘要和前序链哈希，仅撤回本次测试新增的尾记录，原有字节保持；撤回记录及前后摘要保存在原证据目录。入口随后显式设置独立审计目录，并检查运行前后开发审计目录全部文件摘要一致。

最终复验目录：`tmp/postgres-migration-kcgvlllj/`；最终源码审查：`tmp/N7-02-postgres-review-v2-20261112.json`。以该目录 `result.json` 的 `passed`、`development_audit_unchanged`、API 退出和临时库删除结果共同验收。备份清单、实际日志、原生停止回执及恢复后的 API 结果均保留，源码检查点另行记录。

此项新增真实模型调用 **0**、模拟 LLM **0**。证据类别是实际 PostgreSQL、磁盘文件、HTTP、原生命令及迁移失败事务；不把 SQL 旧记录夹具计为实际模型或命令运行。

## 复验与交付边界

在 `platform/server` 下运行 `.venv/Scripts/python.exe postgres_migration_acceptance.py`。复用本地私有配置，凭据不写入报告；审查哈希变化会拒绝执行，需先读源码并更新登记。入口会保存每次独立证据目录，最终清理只针对当次创建且名称经过校验的三个库。

原53项和295项范围继续有效。N7-02归档后主线为46/53，剩7项：N4-05、N4-07、N7-01、N7-03、N7-04、N7-05、N7-06。下一项按原计划推进真实工程算例与报告完整流程（N7-03）；不把本次数据库演练扩大成新的权限或隔离设计阶段。
