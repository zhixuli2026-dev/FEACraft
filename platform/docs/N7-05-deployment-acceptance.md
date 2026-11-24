# N7-05 部署与恢复交接

2026-11-18，N7-05 达到主线计划的退出条件。交接覆盖 Windows/WSL、单机 Docker 默认 FS 和 PostgreSQL/Redis/SeaweedFS 独立 worker 部署。

| 原计划要求 | 操作入口与核验结果 |
|---|---|
| 迁移、开关 | 部署手册列出数据库迁移、存储选择、分布式投递和 provider 密钥配置。默认 FS 从空库执行 Alembic 迁移后，原账户和文件可在新实例读取。见 [部署手册](deployment.md)。 |
| 工具链、权限、编译缓存 | 环境矩阵逐项列出实际配置与工程算例。UDF 使用作业私有编译目录，真实 wmake、加载、两 rank 求解和取消已分别核验。WSL 普通命令以非特权用户执行。见 [部署环境矩阵](deployment-environment-matrix.md)、[编译缓存说明](toolchain-deployment.md)。 |
| 存储、回退、原凭据 | FS 三服务恢复后可独立修改副本。PG/S3 演练恢复八个卷和原 provider 密钥，三份历史 PDF 摘要一致，原配置完成一次真实 DeepSeek 文件读取。存储清单版本迁移、保留与回收有单独的运维入口。见 [联合恢复](deployment-recovery-runbook.md)、[版本操作](storage-version-operations.md)。 |
| 日志、证据路径 | 通过容器日志、job/attempt/session/turn ID 关联服务与数据，证据保留实例、时间及摘要。恢复验收两次准入前失败记录和最终成功记录均可从联合恢复手册定位。 |
| 发布包 | 文件白名单、逐文件 SHA-256 清单、包外校验值和隔离解包命令见 [发布包说明](release-review.md)。本次归档为 `platform/backups/release-review-N7-05-closed-20261118.zip`。部署私密配置、备份卷、凭据与数据库在归档白名单之外。 |

原台账的三个待办分别对应工具依赖与缓存、操作与恢复手册、部署与存储兼容说明，上表列出了各自的运行证据。商业后端、外部资源和多机故障矩阵继续由原功能条目和 N7-01 跟踪。当前真实模型证据来自既有联合恢复演练，本次新增 FS 验收仅运行实际基础设施。
